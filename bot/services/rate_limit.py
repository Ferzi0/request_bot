"""Антифлуд в памяти процесса.

От двойной отправки защищает не он, а очередь апдейтов одного пользователя
(SimpleEventIsolation в bot/app.py).

В памяти — потому что БД пока нет. После перезапуска счётчики обнуляются;
для бота на одной машине это приемлемо (с БД перенесём в неё).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Hashable


class SlidingWindowLimiter:
    """Не более `limit` событий за `window` секунд на ключ (например, user id)."""

    def __init__(
        self,
        limit: int,
        window: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock  # подменяется в тестах, чтобы «перемотать» время
        self._events: dict[Hashable, deque[float]] = {}

    def _actual(self, key: Hashable) -> deque[float]:
        events = self._events.get(key, deque())
        border = self._clock() - self._window
        while events and events[0] <= border:
            events.popleft()
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)  # не копим пустые записи
        return events

    def retry_after(self, key: Hashable) -> float:
        """0 — можно; иначе сколько секунд ждать до освобождения слота."""
        events = self._actual(key)
        if len(events) < self._limit:
            return 0.0
        return max(0.0, events[0] + self._window - self._clock())

    def hit(self, key: Hashable) -> None:
        events = self._actual(key)
        events.append(self._clock())
        self._events[key] = events
