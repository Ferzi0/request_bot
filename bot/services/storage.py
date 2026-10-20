"""Хранилище заявок: протокол, пустая реализация и состояние базы.

Хендлер работает с протоколом ApplicationRepository и не знает, где лежат
заявки. Боевая реализация — SqliteRepository (services/sqlite_repository.py),
подключается в bot/__main__.py. NullRepository — для тестов и для работы
без базы (если её не удалось открыть при старте).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from bot.models import Application, ApplicationRecord, ApplicationStatus


class StatusOutcome(StrEnum):
    """Чем закончилась попытка сменить статус."""

    CHANGED = "changed"
    ALREADY = "already"  # заявка уже в этом статусе — время не сдвигается
    NOT_ALLOWED = "not_allowed"  # переход запрещён (см. ALLOWED_TRANSITIONS)
    NOT_FOUND = "not_found"  # заявки нет в базе


@dataclass(frozen=True, slots=True)
class StatusChange:
    outcome: StatusOutcome
    # Заявка ПОСЛЕ попытки (None — не найдена): хендлеру нужно её время и статус.
    record: ApplicationRecord | None = None


class ApplicationRepository(Protocol):
    @property
    def available(self) -> bool:
        """Есть ли настоящая база. False — бот работает без неё (только пересылка)."""
        ...

    async def add(self, application: Application) -> None:
        """Сохранить заявку. Повтор с тем же номером не должен создавать дубль.

        Не удалось сохранить (в том числе номер занят чужой заявкой) — исключение.
        """
        ...

    async def mark_delivered(self, app_id: str, delivered_at: datetime) -> None:
        """Отметить, что заявка дошла до администратора. Повтор ничего не меняет."""
        ...

    async def get(self, app_id: str) -> ApplicationRecord | None: ...

    async def list_by_status(
        self, status: ApplicationStatus | None, limit: int, offset: int
    ) -> list[ApplicationRecord]:
        """Страница заявок, новые сверху. status=None — все."""
        ...

    async def count_by_status(self) -> dict[ApplicationStatus, int]:
        """Сколько заявок в каждом статусе (все статусы, нули тоже)."""
        ...

    async def set_status(
        self, app_id: str, status: ApplicationStatus, now: datetime
    ) -> StatusChange:
        """Атомарно сменить статус по правилам ALLOWED_TRANSITIONS. Повтор безопасен."""
        ...

    async def claim_client_notice(self, app_id: str, now: datetime) -> int | None:
        """Атомарно поставить метку «клиенту сообщили о работе».

        Возвращает Telegram id клиента, если метку поставил именно этот вызов
        (заявка в работе, метки ещё не было). None — писать клиенту не нужно:
        уже сообщали, заявки нет или она не в работе.
        """
        ...

    async def release_client_notice(self, app_id: str, claimed_at: datetime) -> None:
        """Снять свою метку (временный сбой отправки) — следующий раз можно повторить."""
        ...


class NullRepository:
    """Ничего не хранит: заявка живёт только в сообщении администратору."""

    @property
    def available(self) -> bool:
        return False

    async def add(self, application: Application) -> None:
        return None

    async def mark_delivered(self, app_id: str, delivered_at: datetime) -> None:
        return None

    async def get(self, app_id: str) -> ApplicationRecord | None:
        return None

    async def list_by_status(
        self, status: ApplicationStatus | None, limit: int, offset: int
    ) -> list[ApplicationRecord]:
        return []

    async def count_by_status(self) -> dict[ApplicationStatus, int]:
        return dict.fromkeys(ApplicationStatus, 0)

    async def set_status(
        self, app_id: str, status: ApplicationStatus, now: datetime
    ) -> StatusChange:
        return StatusChange(StatusOutcome.NOT_FOUND)

    async def claim_client_notice(self, app_id: str, now: datetime) -> int | None:
        # Без базы нечем защититься от повторной отправки — клиенту не пишем.
        return None

    async def release_client_notice(self, app_id: str, claimed_at: datetime) -> None:
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
