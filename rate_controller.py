"""
Kiểm soát tốc độ phản hồi:
  - human_delay(): độ trễ ngẫu nhiên mô phỏng người thật đang gõ.
  - RateLimiter: giới hạn số tin gửi mỗi phút cho mỗi page (sliding window).

Bot chăm sóc khách hàng 24/7 — không giới hạn theo giờ hoạt động.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque

from config import settings


async def human_delay() -> None:
    """Chờ ngẫu nhiên giữa min/max giây trước khi gửi, cho tự nhiên và tránh spam."""
    delay = random.uniform(settings.reply_min_delay_seconds, settings.reply_max_delay_seconds)
    await asyncio.sleep(delay)


class RateLimiter:
    """Sliding-window: tối đa N tin trong 60 giây. Nếu vượt, chờ tới khi có slot."""

    def __init__(self, max_per_minute: int) -> None:
        self.max_per_minute = max_per_minute
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                # Bỏ các mốc thời gian cũ hơn 60s
                while self._timestamps and now - self._timestamps[0] >= 60:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_per_minute:
                    self._timestamps.append(now)
                    return

                # Đã đầy quota -> chờ tới khi mốc cũ nhất hết hạn
                wait_for = 60 - (now - self._timestamps[0])
                await asyncio.sleep(max(wait_for, 0.1))


rate_limiter = RateLimiter(settings.max_messages_per_minute)
