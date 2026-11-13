"""有界 429 限流恢复的单元测试（不依赖网络）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.ratelimit import (
    InvalidRetryAfter,
    RateLimitBudgetExceeded,
    RateLimitCoordinator,
    parse_retry_after,
)


# --------------------------------------------------------------------------- #
# parse_retry_after
# --------------------------------------------------------------------------- #
def test_parse_retry_after_delta_seconds():
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after("  0 ") == 0.0
    assert parse_retry_after("120") == 120.0


def test_parse_retry_after_missing_or_empty_is_invalid():
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after(None)
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("")
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("   ")


def test_parse_retry_after_decimal_and_negative_are_invalid():
    # RFC 9110 的 delta-seconds 是非负整数，小数/负数/科学计数法都不接受
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("1.5")
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("-3")
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("1e2")
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("soon")


def test_parse_retry_after_http_date_future():
    now = datetime(2026, 11, 10, 8, 0, 0, tzinfo=timezone.utc)
    header = (now + timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(header, now=now) == pytest.approx(30.0, abs=1.0)


def test_parse_retry_after_http_date_past_returns_zero():
    now = datetime(2026, 11, 10, 8, 0, 0, tzinfo=timezone.utc)
    header = (now - timedelta(seconds=45)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert parse_retry_after(header, now=now) == 0.0


def test_parse_retry_after_bad_http_date_is_invalid():
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("Wed, 99 Xxx 2026 07:28:00 GMT")
    with pytest.raises(InvalidRetryAfter):
        parse_retry_after("2026-11-10T08:00:00")


# --------------------------------------------------------------------------- #
# RateLimitCoordinator
# --------------------------------------------------------------------------- #
async def test_coordinator_no_cooldown_returns_immediately():
    coordinator = RateLimitCoordinator(max_wait=1.0)
    await asyncio.wait_for(coordinator.wait_for_cooldown(), timeout=0.1)


async def test_coordinator_extend_and_wait_recovers_within_budget():
    coordinator = RateLimitCoordinator(max_wait=5.0)
    await coordinator.extend_cooldown(0.2)
    # 冷却已设定，等待应在约 0.2 秒后结束且不报错
    await asyncio.wait_for(coordinator.wait_for_cooldown(), timeout=1.0)


async def test_coordinator_cooldown_can_only_be_extended():
    coordinator = RateLimitCoordinator(max_wait=5.0)
    await coordinator.extend_cooldown(1.0)
    # 更短的新冷却不能缩短已有截止时刻
    await coordinator.extend_cooldown(0.1)

    async def waiter():
        await coordinator.wait_for_cooldown()

    # 若被缩短为 0.1s，这里会在 0.3s 前就返回；1.0s 的冷却应仍在生效
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(waiter(), timeout=0.3)


async def test_coordinator_budget_exceeded_on_extend():
    coordinator = RateLimitCoordinator(max_wait=0.2)
    with pytest.raises(RateLimitBudgetExceeded):
        await coordinator.extend_cooldown(5.0)


async def test_coordinator_budget_exceeded_on_wait():
    coordinator = RateLimitCoordinator(max_wait=0.15)
    # 直接构造“预算不足以覆盖冷却”的情形
    with pytest.raises(RateLimitBudgetExceeded):
        await coordinator.extend_cooldown(2.0)
    with pytest.raises(RateLimitBudgetExceeded):
        await coordinator.wait_for_cooldown()


async def test_coordinator_zero_budget_fails_fast():
    coordinator = RateLimitCoordinator(max_wait=0.0)
    with pytest.raises(RateLimitBudgetExceeded):
        await coordinator.extend_cooldown(0.01)
    with pytest.raises(RateLimitBudgetExceeded):
        await coordinator.wait_for_cooldown()


async def test_coordinator_shared_cooldown_gates_concurrent_tasks():
    """所有并发任务共用同一冷却截止时刻：冷却结束前任何任务都不能发请求。"""
    coordinator = RateLimitCoordinator(max_wait=5.0)
    await coordinator.extend_cooldown(0.5)

    started_at = asyncio.get_running_loop().time()
    release_times = []

    async def worker():
        await coordinator.wait_for_cooldown()
        release_times.append(asyncio.get_running_loop().time() - started_at)

    await asyncio.gather(*(worker() for _ in range(4)))
    assert len(release_times) == 4
    # 四个任务都应在冷却结束（约 0.5s）后才被放行，而不是提前陆续发出
    assert min(release_times) >= 0.45


async def test_coordinator_later_429_extends_wait_for_pending_tasks():
    """后续 429 延长冷却时，正在等待的任务必须等到新的截止时刻。"""
    coordinator = RateLimitCoordinator(max_wait=5.0)
    await coordinator.extend_cooldown(0.3)

    async def extend_later():
        await asyncio.sleep(0.15)
        await coordinator.extend_cooldown(0.5)  # 新截止时刻约为 0.15 + 0.5 = 0.65

    async def waiter():
        await coordinator.wait_for_cooldown()
        return asyncio.get_running_loop().time() - started_at

    started_at = asyncio.get_running_loop().time()
    _, elapsed = await asyncio.gather(extend_later(), waiter())
    assert elapsed >= 0.6
