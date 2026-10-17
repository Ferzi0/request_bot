"""Middleware уровня диспетчера."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class PrivateChatOnlyMiddleware(BaseMiddleware):
    """Пропускает только личные чаты; в группах и каналах бот молчит.

    Почему middleware, а не фильтр в каждом роутере: правило одно на весь бот,
    и его нельзя случайно забыть в новом роутере.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message is not None:
            chat = event.message.chat
        if chat is None or chat.type != ChatType.PRIVATE:
            logger.debug("Пропущен апдейт не из личного чата")
            return None
        return await handler(event, data)
