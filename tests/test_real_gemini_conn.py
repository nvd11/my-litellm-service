"""Opt-in integration test for a real Gemini API request.

Run explicitly with::

    RUN_REAL_GEMINI_TESTS=1 uv run pytest -q tests/test_real_gemini_conn.py -s

The test is skipped during the normal unit-test run to avoid accidental API
charges. HTTP_PROXY/HTTPS_PROXY, when present, are inherited by LiteLLM.
"""

import os

import pytest
from litellm import acompletion
from litellm.exceptions import APIError, RateLimitError

from app.core.config import get_settings


@pytest.mark.asyncio
async def test_real_gemini_chat_completion() -> None:
    """Send a minimal real request and validate the OpenAI-compatible result."""

    if os.getenv("RUN_REAL_GEMINI_TESTS") != "1":
        pytest.skip("set RUN_REAL_GEMINI_TESTS=1 to run the paid integration test")

    settings = get_settings()
    api_key = settings.openai_api_key_free_1.get_secret_value()
    if not api_key or api_key.startswith("replace-with-"):
        pytest.skip("OPENAI_API_KEY_FREE_1 is not configured with a real Gemini API key")

    os.environ.setdefault("OPENAI_API_KEY_FREE_1", api_key)

    try:
        response = await acompletion(
            model="gemini/gemini-3.7-flash",
            api_key=api_key,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            # max_tokens=64,  # Output token limit; Gemini's maximum is 64K.
            # timeout=30,
        )
    except RateLimitError as error:
        pytest.fail(f"Gemini returned HTTP 429 Rate Limit: {error}")
    except APIError as error:
        status_code = getattr(error, "status_code", None)
        if status_code is not None and 500 <= status_code <= 599:
            pytest.fail(f"Gemini returned HTTP {status_code} server error: {error}")
        raise

    assert response.choices
    print(f"LLM response: {response.choices[0].message.content!r}")
    print(f"LLM model: {response.model}")
    print(f"LLM usage: {response.usage}")

    assert response.choices[0].message.role == "assistant"
    assert response.model
    assert response.usage is not None
