"""稳健性工具 — 熔断器 + 带退避的重试

用于把「LLM API 网络抖动直接崩掉整个任务」变成可恢复：
- retry_call / aretry_call：瞬时失败时按指数退避重试若干次
- CircuitBreaker：连续失败到阈值后「熔断」，在恢复窗口内快速失败，避免雪崩

均为通用工具，无第三方依赖。
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from .exceptions import AgentsException


class CircuitBreakerOpen(AgentsException):
    """熔断器处于打开状态，拒绝调用。"""


def retry_call(
    fn: Callable[[], Any],
    *,
    max_retries: int,
    backoff: float,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> Any:
    """同步重试：调用 fn()，失败按指数退避重试，最多 max_retries 次额外尝试。"""
    attempt = 0
    while True:
        try:
            return fn()
        except CircuitBreakerOpen:
            raise  # 熔断不重试
        except Exception as exc:
            if attempt >= max_retries:
                raise
            if on_retry is not None:
                on_retry(attempt + 1, exc)
            if backoff > 0:
                time.sleep(backoff * (2 ** attempt))
            attempt += 1


async def aretry_call(
    make_coro: Callable[[], Awaitable[Any]],
    *,
    max_retries: int,
    backoff: float,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> Any:
    """异步重试：每次尝试调用 make_coro() 取得新协程并 await。"""
    attempt = 0
    while True:
        try:
            return await make_coro()
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            if attempt >= max_retries:
                raise
            if on_retry is not None:
                on_retry(attempt + 1, exc)
            if backoff > 0:
                await asyncio.sleep(backoff * (2 ** attempt))
            attempt += 1


class CircuitBreaker:
    """简单熔断器：连续失败到阈值后打开，恢复窗口后进入半开试探。

    状态：
      - closed   ：正常放行
      - open     ：拒绝放行；超过 recovery_timeout 后转 half_open
      - half_open：放行一次试探；成功则 closed，失败则重新 open
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 300) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._state = self.CLOSED
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN and (
            time.time() - self._opened_at >= self.recovery_timeout
        ):
            self._state = self.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        """是否放行本次调用。"""
        return self.state != self.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._state = self.OPEN
            self._opened_at = time.time()
