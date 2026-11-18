"""独立依赖熔断器：并发半开只允许一次探测，只重试临时网络错误。"""

import random
import threading
import time
from collections.abc import Callable

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


class CircuitOpen(RuntimeError):
    pass


def transient(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {429, 502, 503, 504}
    return isinstance(error, (TimeoutError, ConnectionError, httpx.TransportError,
                              RedisConnectionError, RedisTimeoutError))


class CircuitBreaker:
    def __init__(self, name: str, threshold=3, cooldown=10.0, clock=time.monotonic):
        if threshold < 1 or cooldown < 0:
            raise ValueError("invalid breaker configuration")
        self.name, self.threshold, self.cooldown, self.clock = name, threshold, cooldown, clock
        self.failures = 0
        self.opened_at = None
        self.probing = False
        self.generation = 0
        self.lock = threading.Lock()

    def call(self, function: Callable):
        with self.lock:
            if self.opened_at is not None:
                if self.clock() - self.opened_at < self.cooldown or self.probing:
                    raise CircuitOpen(f"{self.name} temporarily unavailable")
                self.probing = True
            generation = self.generation
        try:
            result = function()
        except BaseException as exc:
            with self.lock:
                if generation == self.generation:
                    if isinstance(exc, Exception) and transient(exc):
                        self.failures += 1
                        if self.probing or self.failures >= self.threshold:
                            self.opened_at = self.clock()
                            self.generation += 1
                    self.probing = False
            raise
        with self.lock:
            if generation == self.generation:
                self.failures, self.opened_at, self.probing = 0, None, False
        return result


def retry_call(breaker: CircuitBreaker, function: Callable, attempts=2, sleep=time.sleep):
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(attempts):
        try:
            return breaker.call(function)
        except Exception as exc:
            if not transient(exc) or attempt == attempts - 1:
                raise
            sleep(min(0.05 * 2 ** attempt, 0.2) + random.uniform(0, 0.02))
