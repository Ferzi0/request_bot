"""Глобальный обработчик исключений из хендлеров."""

from __future__ import annotations

import logging
from contextlib import suppress

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import ErrorEvent

from bot import texts

logger = logging.getLogger(__name__)


async def on_error(event: ErrorEvent, bot: Bot) -> None:
    update = event.update
    if isinstance(event.exception, TelegramForbiddenError):
        # Человек заблокировал бота — штатная ситуация, а не сбой: без трейсбека,
        # и писать ему всё равно нельзя.
        logger.info("Пользователь заблокировал бота (update_id=%s)", update.update_id)
        return
    # В лог — тип ошибки и трейсбек для разработчика; текст апдейта не пишем.
    logger.error(
        "Ошибка при обработке update_id=%s: %s",
        update.update_id,
        type(event.exception).__name__,
        exc_info=event.exception,
    )
    chat_id: int | None = None
    if update.message is not None:
        chat_id = update.message.chat.id
    elif update.callback_query is not None:
        with suppress(TelegramAPIError):
            await update.callback_query.answer()
        if update.callback_query.message is not None:
            chat_id = update.callback_query.message.chat.id
    if chat_id is None:
        return
    # Если не получится и это (например, сеть упала) — просто молчим: ошибка уже в логе.
    with suppress(TelegramAPIError):
        await bot.send_message(chat_id, texts.UNEXPECTED_ERROR)


def create_router() -> Router:
    """Обработчик исключений."""
    router = Router(name="errors")
    router.errors.register(on_error)
    return router
