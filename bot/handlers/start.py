"""/start, в том числе с deep link: t.me/<бот>?start=site."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot import texts
from bot.drafts import reset_form
from bot.keyboards import apply_keyboard
from bot.models import Source

logger = logging.getLogger(__name__)

# Метка в ссылке с сайта. Сравниваем точно: payload приходит от пользователя.
SITE_PAYLOAD = "site"


async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    source = Source.SITE if command.args == SITE_PAYLOAD else Source.BOT
    await reset_form(state, source=source)
    logger.info("start: user=%s source=%s", message.chat.id, source.value)
    await message.answer(texts.GREETING, reply_markup=apply_keyboard())


def create_router() -> Router:
    """/start с необязательным deep link."""
    router = Router(name="start")
    router.message.register(cmd_start, CommandStart())
    return router
