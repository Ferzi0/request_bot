"""Хранилище заявок: протокол, пустая реализация и состояние базы.

Хендлер работает с протоколом ApplicationRepository и не знает, где лежат
заявки. Боевая реализация — SqliteRepository (services/sqlite_repository.py),
подключается в bot/__main__.py. NullRepository — для тестов и для работы
без базы (если её не удалось открыть при старте).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from bot.models import Application


class ApplicationRepository(Protocol):
    async def add(self, application: Application) -> None:
        """Сохранить заявку. Повтор с тем же номером не должен создавать дубль.

        Не удалось сохранить (в том числе номер занят чужой заявкой) — исключение.
        """
        ...

    async def mark_delivered(self, app_id: str, delivered_at: datetime) -> None:
        """Отметить, что заявка дошла до администратора. Повтор ничего не меняет."""
        ...


class NullRepository:
    """Ничего не сохраняет: заявка живёт только в сообщении администратору."""

    async def add(self, application: Application) -> None:
        return None

    async def mark_delivered(self, app_id: str, delivered_at: datetime) -> None:
        return None


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """Состояние базы при старте — для сообщения администратору.

    error — имя класса ошибки, если база не открылась (без текста исключения:
    в нём бывают пути с именем пользователя Mac).
    """

    error: str | None = None
    schema_version: int = 0
    applications: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None
