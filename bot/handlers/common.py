"""Общие команды (/help, /cancel, «Отмена») и ответы на всё непредусмотренное."""

from __future__ import annotations

import logging
from contextlib import suppress

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.callbacks import EditFieldCallback, FormAction, FormCallback, ProjectTypeCallback
from bot.drafts import reset_form
from bot.keyboards import apply_keyboard
from bot.services.rate_limit import SlidingWindowLimiter

# Кнопки шагов диалога: только для них подсказка «диалог был прерван» имеет смысл.
# («Оставить заявку» и «Отмена» обрабатываются без состояния и сюда не доходят.)
_DIALOG_PREFIXES = (
    FormCallback.__prefix__,
    ProjectTypeCallback.__prefix__,
    EditFieldCallback.__prefix__,
)

logger = logging.getLogger(__name__)

COMMAND_RE = r"^/[A-Za-z0-9_]+(@\w+)?(\s|$)"


async def drop_keyboard(callback: CallbackQuery) -> None:
    """Убирает кнопки под нажатым сообщением, чтобы старые шаги нельзя было нажать.

    Ошибки игнорируем: сообщение могло быть уже изменено или недоступно —
    это не повод прерывать сценарий.
    """
    if isinstance(callback.message, Message):
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)


async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=apply_keyboard())


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL, reply_markup=apply_keyboard())
        return
    await reset_form(state)
    logger.info("cancel: user=%s via command", message.chat.id)
    await message.answer(texts.CANCELLED, reply_markup=apply_keyboard())


async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await drop_keyboard(callback)
    await reset_form(state)
    logger.info("cancel: user=%s via button", callback.from_user.id)
    if isinstance(callback.message, Message):
        await callback.message.answer(texts.CANCELLED, reply_markup=apply_keyboard())


async def unknown_command(message: Message) -> None:
    # Без этого «/foo» на текстовом шаге сохранилась бы как имя или задача.
    await message.answer(texts.UNKNOWN_COMMAND)


async def any_message(message: Message) -> None:
    await message.answer(texts.FALLBACK, reply_markup=apply_keyboard())


async def any_callback(
    callback: CallbackQuery, state: FSMContext, restart_hints: SlidingWindowLimiter
) -> None:
    # Кнопка из прошлого шага или от старой заявки: ничего не делаем, но и не «висим».
    await callback.answer(texts.STALE_BUTTON)
    await drop_keyboard(callback)
    # Бот ничего не знает о человеке (ни шага, ни данных), а нажата кнопка шага —
    # значит, диалог потерян: бот перезапускался, а шаги хранятся в памяти. Одной
    # всплывашки мало — человек не поймёт, что делать.
    # Данные проверяем тоже: после отправки или отмены шага нет, но reset_form оставляет
    # черновик с источником — там второй тап по «Отправить» не «прерванный диалог».
    # Если шаг есть — это старый экран или двойной тап: хватает всплывашки.
    if not _is_dialog_button(callback.data):
        return
    if await state.get_state() is not None or await state.get_data():
        return
    user_id = callback.from_user.id
    # Не чаще раза в минуту: три быстрых нажатия старых кнопок — одна подсказка.
    # Очередь апдейтов одного пользователя (SimpleEventIsolation) делает проверку и
    # отметку атомарными без замков.
    if callback.message is None or restart_hints.retry_after(user_id):
        return
    restart_hints.hit(user_id)
    try:
        await callback.message.answer(texts.DIALOG_INTERRUPTED, reply_markup=apply_keyboard())
    except TelegramAPIError as exc:
        # Например, человек заблокировал бота: подсказка — не повод для ERROR.
        logger.info("Подсказка о прерванном диалоге не отправлена: %s", type(exc).__name__)


def _is_dialog_button(data: str | None) -> bool:
    return data is not None and data.split(":", 1)[0] in _DIALOG_PREFIXES


def create_commands_router() -> Router:
    """Команды и «Отмена», работающие на любом шаге."""
    router = Router(name="commands")
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.callback_query.register(cb_cancel, FormCallback.filter(F.action == FormAction.CANCEL))
    # Любая другая команда (/start, /help, /cancel уже разобраны выше). Шаблон — как
    # у Telegram: «/» и латиница/цифры/«_», возможно с @ботом; «/ текст» командой не считается.
    router.message.register(unknown_command, F.text.regexp(COMMAND_RE))
    return router


def create_fallback_router() -> Router:
    """Ответы на всё, что не подошло другим роутерам. Подключать последним."""
    router = Router(name="fallback")
    router.message.register(any_message)
    router.callback_query.register(any_callback)
    return router
