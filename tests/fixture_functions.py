"""Test-only registered functions. Not named test_*.py so pytest doesn't collect it."""

from __future__ import annotations

import asyncio
from collections import Counter

from maw.functions import register_function

_calls: Counter[str] = Counter()


@register_function("test.sleep")
async def sleep(seconds: float = 0.1, tag: str = "") -> dict:
    """Sleep, then report."""
    await asyncio.sleep(seconds)
    return {"slept": seconds, "tag": tag}


@register_function("test.flaky")
def flaky(key: str, fail_times: int = 1) -> dict:
    """Fail until called more than ``fail_times`` times for ``key``."""
    _calls[key] += 1
    if _calls[key] <= fail_times:
        raise RuntimeError(f"flaky failure {_calls[key]} for {key}")
    return {"attempts": _calls[key]}


@register_function("test.boom")
def boom() -> dict:
    """Always fail."""
    raise ValueError("boom")
