"""Safe smoke checks for a running LiteLLM Proxy."""

import argparse
import asyncio
import os
from typing import Any

import httpx


def _auth_headers(master_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }


async def get_health(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get("/health")
    response.raise_for_status()
    payload = response.json()
    return {"status_code": response.status_code, "response_type": type(payload).__name__}


async def get_models(client: httpx.AsyncClient) -> set[str]:
    response = await client.get("/v1/models")
    response.raise_for_status()
    payload = response.json()
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise ValueError("/v1/models response does not contain a data list")
    model_ids = {item.get("id") for item in models if isinstance(item, dict)}
    if None in model_ids:
        model_ids.remove(None)
    return {model_id for model_id in model_ids if isinstance(model_id, str)}


async def send_chat(client: httpx.AsyncClient, model: str, prompt: str) -> dict[str, Any]:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
        },
    )
    response.raise_for_status()
    payload = response.json()
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return {
        "status_code": response.status_code,
        "model": payload.get("model") if isinstance(payload, dict) else None,
        "has_usage": isinstance(usage, dict),
    }


async def run(base_url: str, master_key: str, model: str | None, send_chat_request: bool) -> int:
    headers = _auth_headers(master_key)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15.0) as client:
        health = await get_health(client)
        models = await get_models(client)
        print(f"health: {health}")
        print(f"models: {sorted(models)}")

        if not send_chat_request:
            return 0
        if not model:
            print("A model is required when --send-chat is specified.")
            return 1
        result = await send_chat(client, model, "Reply with the single word: OK")
        print(f"chat: {result}")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4000")
    parser.add_argument("--model")
    parser.add_argument("--send-chat", action="store_true")
    args = parser.parse_args()

    master_key = os.getenv("LITELLM_MASTER_KEY")
    if not master_key:
        print("LITELLM_MASTER_KEY is required.")
        return 1

    try:
        return asyncio.run(run(args.base_url, master_key, args.model, args.send_chat))
    except httpx.HTTPError as error:
        print(f"Proxy request failed: {type(error).__name__}")
        return 1
    except (ValueError, TypeError):
        print("Proxy response validation failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

