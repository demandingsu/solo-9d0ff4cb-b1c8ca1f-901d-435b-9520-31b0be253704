"""有界 429 限流恢复：Retry-After 解析 + 并发详情共享冷却。

设计要点（对应需求）：
* 每次查询拥有一个共享的 :class:`RateLimitCoordinator`，所有并发详情任务共用
  同一个“冷却截止时刻”：任一详情收到 429 即设定冷却，尚未发出请求的任务在
  发请求前先等冷却结束；后续 429 只能把冷却截止时刻继续向后延长。
* 等待有总预算（``max_wait``，秒）：预算在同一查询的所有任务间共享，
  总等待时间不会超过该上限；预算耗尽即判定该详情失败，由上层按待核实处理。
* ``Retry-After`` 支持两种合法形式（RFC 9110）：
  - delta-seconds：非负整数秒；
  - HTTP 日期：如 ``Wed, 21 Oct 2026 07:28:00 GMT``。
  缺失、无法解析、负数等无效值统一抛 :class:`InvalidRetryAfter`，上层按失败处理。
"""

from __future__ import annotations

import asyncio
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from time import monotonic


class InvalidRetryAfter(ValueError):
    """Retry-After 头缺失或无法解析为秒数/HTTP 日期。"""


class RateLimitBudgetExceeded(Exception):
    """有界等待预算已耗尽，不能再为 429 冷却等待。"""


def parse_retry_after(header: str | None, now: datetime | None = None) -> float:
    """把 Retry-After 头解析为需要等待的秒数（非负）。

    优先按 delta-seconds（非负整数）解析，失败再按 HTTP-date 解析；
    HTTP 日期若已过期，返回 ``0.0``（无需等待即可立即重试）。
    任何无法解析的形式都抛 :class:`InvalidRetryAfter`，由调用方按失败处理。
    """
    if header is None:
        raise InvalidRetryAfter("响应缺少 Retry-After 头")
    value = header.strip()
    if not value:
        raise InvalidRetryAfter("Retry-After 头为空")

    # 1) delta-seconds：RFC 要求是非负整数，不接受小数/负数/科学计数法
    if value.isdigit():
        return float(int(value))

    # 2) HTTP-date
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise InvalidRetryAfter(f"无法解析的 Retry-After：{header!r}") from exc
    if retry_at is None:
        raise InvalidRetryAfter(f"无法解析的 Retry-After：{header!r}")
    if retry_at.tzinfo is None:
        # 合规的 HTTP-date 必带 GMT 时区；缺失时区属于无效头
        raise InvalidRetryAfter(f"Retry-After 日期缺少时区信息：{header!r}")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0.0, (retry_at.astimezone(timezone.utc) - current).total_seconds())


class RateLimitCoordinator:
    """同一查询内所有详情任务共享的冷却状态与等待预算。

    时间基准统一使用 :func:`time.monotonic`，不受系统时钟回调影响。
    所有状态在 :attr:`_condition` 锁内访问，等待通过带超时的
    ``Condition.wait`` 完成，冷却被后续 429 延长时等待方会被唤醒并重新评估。
    """

    def __init__(self, max_wait: float):
        self._condition = asyncio.Condition()
        self._cooldown_until: float | None = None  # monotonic 时刻
        self._deadline = monotonic() + max(0.0, max_wait)

    async def wait_for_cooldown(self) -> None:
        """在发出请求前等待共享冷却结束；预算耗尽则立即失败。"""
        async with self._condition:
            while True:
                now = monotonic()
                self._raise_if_budget_exhausted(now)
                if self._cooldown_until is None or now >= self._cooldown_until:
                    return
                # Python 3.11 的 asyncio.Condition.wait 不支持 timeout（3.12 才加入），
                # 用 asyncio.wait_for 包裹：超时（冷却自然结束）或被 extend_cooldown
                # 的 notify_all 唤醒（冷却被延长）都会返回，循环内重新评估
                timeout = min(self._cooldown_until - now, self._deadline - now)
                if timeout <= 0:
                    raise RateLimitBudgetExceeded
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass  # 等待窗口结束，回到循环顶部重新判断冷却/预算

    async def extend_cooldown(self, seconds: float) -> None:
        """收到新的 429 后，按 Retry-After 延长（或首次设定）共享冷却截止时刻。

        冷却只能被延长不能缩短；若剩余等待预算不足以覆盖本次冷却，则判定预算耗尽。
        """
        if seconds < 0:
            raise InvalidRetryAfter("Retry-After 秒数不能为负")
        async with self._condition:
            now = monotonic()
            self._raise_if_budget_exhausted(now)
            new_until = now + seconds
            if self._cooldown_until is None or new_until > self._cooldown_until:
                self._cooldown_until = new_until
            self._condition.notify_all()
            if self._cooldown_until > self._deadline:
                raise RateLimitBudgetExceeded

    def _raise_if_budget_exhausted(self, now: float | None = None) -> None:
        current = monotonic() if now is None else now
        if current >= self._deadline:
            raise RateLimitBudgetExceeded
