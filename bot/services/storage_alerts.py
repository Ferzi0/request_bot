"""Сбои базы заявок: тихая запись и уведомления администратору.

База — архив, главное — доставка заявки в Telegram. Поэтому ошибки базы не
прерывают отправку: они пишутся в лог (каждая) и сообщаются администратору
в чат с ботом (не чаще раза в час на класс ошибки — чтобы при «лежащей» базе
не получать по сообщению на каждую заявку).

В лог и в уведомления попадают только номер заявки и имя класса ошибки:
текст исключения и трейсбек могут содержать данные заявки или пути на Mac.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from html import escape

from aiogram import Bot

from bot import texts
from bot.models import Application
from bot.services.storage import ApplicationRepository

logger = logging.getLogger(__name__)

ALERT_INTERVAL = 3600.0  # секунды: одно уведомление в час на класс ошибки


async def save_quietly(repository: ApplicationRepository, app: Application) -> str | None:
    """Сохраняет заявку. Возвращает имя класса ошибки или None, если всё хорошо."""
    try:
        await repository.add(app)
    except Exception as exc:
        error = type(exc).__name__
        logger.error("Заявка %s НЕ сохранена в базу: %s", app.id, error)
        return error
    return None


async def mark_delivered_quietly(
    repository: ApplicationRepository, app_id: str, delivered_at: datetime
) -> str | None:
    """Ставит отметку о доставке. Возвращает имя класса ошибки или None."""
    try:
        await repository.mark_delivered(app_id, delivered_at)
    except Exception as exc:
        error = type(exc).__name__
        logger.error("Заявка %s: отметка о доставке НЕ записана в базу: %s", app_id, error)
        return error
    return None


class StorageAlerts:
    """Сообщения администратору о сбоях базы с ограничением частоты (в памяти)."""

    def __init__(
        self,
        admin_id: int,
        interval: float = ALERT_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._admin_id = admin_id
        self._interval = interval
        self._clock = clock  # подменяется в тестах, чтобы «прошёл час»
        self._last_sent: dict[str, float] = {}

    async def save_failed(self, bot: Bot, app_id: str, error: str) -> None:
        text = texts.ADMIN_DB_SAVE_FAILED.format(app_id=escape(app_id), error=escape(error))
        await self._send(bot, error, text)

    async def mark_failed(self, bot: Bot, app_id: str, error: str) -> None:
        text = texts.ADMIN_DB_MARK_FAILED.format(app_id=escape(app_id), error=escape(error))
        await self._send(bot, error, text)

    async def _send(self, bot: Bot, error: str, text: str) -> None:
        """Отправляет, если по этому классу ошибки не писали последний час. Не бросает."""
        now = self._clock()
        previous = self._last_sent.get(error)
        if previous is not None and now - previous < self._interval:
            logger.info("Уведомление о сбое базы (%s) пропущено: уже было за последний час", error)
            return
        # Отмечаем ДО await: два одновременных сбоя не дадут двух сообщений.
        self._last_sent[error] = now
        try:
            await bot.send_message(self._admin_id, text)
        except Exception as exc:
            # Не дошло — не считаем отправленным, следующий сбой попробует снова.
            if previous is None:
                self._last_sent.pop(error, None)
            else:
                self._last_sent[error] = previous
            logger.error("Не удалось уведомить администратора о сбое базы: %s", type(exc).__name__)
