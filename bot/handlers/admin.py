"""Кнопки под заявкой у администратора."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import texts
from bot.callbacks import AdminCallback
from bot.config import Settings
from bot.services.formatting import format_local_time

logger = logging.getLogger(__name__)


def _without_callback_buttons(markup: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup | None:
    """Оставляет только кнопки-ссылки («Написать клиенту»), убирая «В работу»."""
    if markup is None:
        return None
    rows = [[button for button in row if button.url] for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _has_callback_buttons(markup: InlineKeyboardMarkup | None) -> bool:
    return markup is not None and any(
        button.callback_data for row in markup.inline_keyboard for button in row
    )


async def cb_take(
    callback: CallbackQuery, callback_data: AdminCallback, settings: Settings
) -> None:
    # Callback может прислать кто угодно, кто видит кнопку (например, если сообщение
    # переслали). Права проверяем по user id нажавшего, а не по чату.
    if callback.from_user.id != settings.admin_id:
        # %r: app_id пришёл в callback_data от постороннего — перевод строки в нём
        # не должен подделать строку лога.
        logger.warning(
            "admin: отказ в доступе user=%s app=%r", callback.from_user.id, callback_data.app_id
        )
        await callback.answer(texts.ADMIN_TAKE_DENIED, show_alert=True)
        return
    if not isinstance(callback.message, Message):
        await callback.answer()
        return
    if not _has_callback_buttons(callback.message.reply_markup):
        # Telegram присылает сообщение в текущем виде: кнопки «В работу» уже нет,
        # значит, нажали второй раз (или на старом экране). Вторую отметку не ставим.
        await callback.answer(texts.ADMIN_TAKEN_ALERT)
        return

    mark = texts.ADMIN_TAKEN_MARK.format(
        taken_at=format_local_time(datetime.now(UTC), settings.tz),
    )
    # html_text восстанавливает разметку из entities, поэтому жирный текст и
    # ссылка tg://user сохраняются, а пользовательские данные остаются экранированными.
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n{mark}",
        reply_markup=_without_callback_buttons(callback.message.reply_markup),
    )
    logger.info("admin: заявка %s взята в работу", callback_data.app_id)
    await callback.answer(texts.ADMIN_TAKEN_ALERT)


def create_router() -> Router:
    """Кнопки под заявками администратора."""
    router = Router(name="admin")
    router.callback_query.register(cb_take, AdminCallback.filter(F.action == "take"))
    return router
