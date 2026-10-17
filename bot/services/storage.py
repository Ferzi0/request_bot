"""Хранилище заявок — точка расширения.

Сейчас заявки только пересылаются администратору, БД нет. Хендлер работает
с протоколом ApplicationRepository, поэтому SQLite/Postgres добавятся
новым классом и одной строкой в bot/app.py — без правки хендлеров.
"""

from __future__ import annotations

from typing import Protocol

from bot.models import Application


class ApplicationRepository(Protocol):
    async def add(self, application: Application) -> None: ...


class NullRepository:
    """Ничего не сохраняет: заявка живёт только в сообщении администратору."""

    async def add(self, application: Application) -> None:
        return None
