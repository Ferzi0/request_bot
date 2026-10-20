"""Кнопки под заявкой у администратора.

«✅ В работу» отмечает заявку и в сообщении, и в базе (статус in_work). Сообщение —
главное: его отметка ставится всегда, даже если база недоступна; о сбое базы
администратор узнаёт отдельным уведомлением (StorageAlerts).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import texts
from bot.callbacks import AdminCallback
from bot.config import Settings
from bot.models import ApplicationStatus
from bot.services.formatting import format_local_time
from bot.services.storage import ApplicationRepository, StatusOutcome
from bot.services.storage_alerts import StorageAlerts

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
    callback: CallbackQuery,
    callback_data: AdminCallback,
    bot: Bot,
    settings: Settings,
    repository: ApplicationRepository,
    storage_alerts: StorageAlerts,
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

    app_id = callback_data.app_id
    # Одно «сейчас» и для базы, и для отметки в сообщении — время совпадает.
    taken_at = datetime.now(UTC)
    popup = texts.ADMIN_TAKEN_ALERT
    db_error: str | None = None
    if not repository.available:
        db_error = texts.DB_UNAVAILABLE_REASON
    else:
        try:
            change = await repository.set_status(app_id, ApplicationStatus.IN_WORK, taken_at)
        except Exception as exc:
            db_error = type(exc).__name__
            logger.error("admin: заявка %r — статус in_work не записан: %s", app_id, db_error)
        else:
            record = change.record
            if change.outcome is StatusOutcome.NOT_FOUND:
                # Заявку не удалось сохранить при отправке — честно говорим об этом.
                popup = texts.ADMIN_TAKEN_NOT_IN_DB
            elif change.outcome is StatusOutcome.ALREADY and record and record.taken_at:
                # Уже взяли в работу из панели: в сообщении — то же время, что в базе.
                taken_at = record.taken_at
            elif change.outcome is StatusOutcome.NOT_ALLOWED and record:
                # Заявку уже закрыли в панели — «В работе» в сообщении было бы неправдой.
                await callback.answer(
                    texts.ADMIN_TAKE_NOT_ALLOWED.format(status=record.status.label),
                    show_alert=True,
                )
                return

    mark = texts.ADMIN_TAKEN_MARK.format(taken_at=format_local_time(taken_at, settings.tz))
    # html_text восстанавливает разметку из entities, поэтому жирный текст и
    # ссылка tg://user сохраняются, а пользовательские данные остаются экранированными.
    await callback.message.edit_text(
        f"{callback.message.html_text}\n\n{mark}",
        reply_markup=_without_callback_buttons(callback.message.reply_markup),
    )
    logger.info("admin: заявка %r взята в работу", app_id)
    await callback.answer(popup)
    if db_error is not None:
        await storage_alerts.status_failed(bot, app_id, ApplicationStatus.IN_WORK, db_error)


def create_router() -> Router:
    """Кнопки под заявками администратора."""
    router = Router(name="admin")
    router.callback_query.register(cb_take, AdminCallback.filter(F.action == "take"))
    return router
