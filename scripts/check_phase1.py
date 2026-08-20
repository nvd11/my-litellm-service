"""Run the Phase 1 MySQL and Redis connectivity checks."""

import asyncio

from app.core.config import get_settings, redacted_summary
from app.core.connectivity import check_all


async def run_checks() -> int:
    settings = get_settings()
    print(f"Configuration: {redacted_summary(settings)}")
    results = await check_all(settings)

    print("Dependency | Status | Latency (ms) | Detail")
    print("-----------|--------|---------------|--------------------")
    for result in results:
        status = "OK" if result.ok else "FAIL"
        latency = "-" if result.latency_ms is None else f"{result.latency_ms:.2f}"
        print(f"{result.name:<10} | {status:<6} | {latency:>13} | {result.detail}")

    return 0 if all(result.ok for result in results) else 1


def main() -> int:
    try:
        return asyncio.run(run_checks())
    except Exception:  # noqa: BLE001 - CLI must not print .env or DSNs.
        print("Configuration or connectivity check failed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

