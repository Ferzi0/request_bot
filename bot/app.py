"""Сборка приложения: Bot, Dispatcher и зависимости.

Фабрики вынесены из __main__, чтобы тесты собирали ровно тот же диспетчер,
что и боевой запуск, — с подменённой только сетевой сессией.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from bot import texts
from bot.config import Settings
from bot.handlers import get_routers
from bot.middlewares import PrivateChatOnlyMiddleware
from bot.services.notifier import AdminNotifier, Sleep
from bot.services.rate_limit import SlidingWindowLimiter
from bot.services.storage import ApplicationRepository, NullRepository
from bot.services.validation import Limits

logger = logging.getLogger(__name__)


def create_bot(settings: Settings, session: BaseSession | None = None) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        # HTML, а не Markdown: экранировать нужно всего три символа (& < >),
        # и html.escape из стандартной библиотеки делает это надёжно.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher(
    settings: Settings,
    *,
    repository: ApplicationRepository | None = None,
    sleep: Sleep | None = None,
) -> Dispatcher:
    # MemoryStorage: шаги диалога теряются при перезапуске — приемлемо, пока нет БД.
    # SimpleEventIsolation: апдейты ОДНОГО пользователя обрабатываются строго по очереди
    # (замок на его ключ FSM), разных пользователей — параллельно. Без этого два быстрых
    # сообщения или двойное нажатие кнопки читают одно и то же состояние: имя и почта,
    # отправленные подряд, обе записались бы как имя, а «Отправить» ушла бы дважды.
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())

    # workflow_data: всё, что положено сюда, хендлеры получают по имени параметра.
    dp["settings"] = settings
    dp["limits"] = Limits(
        name_min=settings.name_min,
        name_max=settings.name_max,
        contact_max=settings.contact_max,
        task_min=settings.task_min,
        task_max=settings.task_max,
    )
    notifier_kwargs = {"sleep": sleep} if sleep else {}
    dp["notifier"] = AdminNotifier(
        settings.admin_id, settings.tz, settings.send_retry_delays, **notifier_kwargs
    )
    dp["limiter"] = SlidingWindowLimiter(settings.applications_per_hour, window=3600)
    dp["repository"] = repository or NullRepository()

    private_only = PrivateChatOnlyMiddleware()
    dp.message.outer_middleware(private_only)
    dp.callback_query.outer_middleware(private_only)

    dp.include_routers(*get_routers())
    dp.startup.register(on_startup)
    return dp


async def on_startup(bot: Bot, settings: Settings) -> None:
    """Меню команд и проверочное сообщение администратору. Ошибки здесь не фатальны."""
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description=texts.CMD_START),
                BotCommand(command="help", description=texts.CMD_HELP),
                BotCommand(command="cancel", description=texts.CMD_CANCEL),
            ],
            scope=BotCommandScopeAllPrivateChats(),
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось установить меню команд: %s", type(exc).__name__)

    if settings.notify_admin_on_start:
        try:
            await bot.send_message(settings.admin_id, texts.ADMIN_STARTED)
        except TelegramAPIError as exc:
            # Частая причина: администратор ещё не открыл бота и не нажал /start —
            # бот не может написать первым. Тогда заявки тоже не дойдут.
            logger.error(
                "Не удалось написать администратору (ADMIN_ID): %s: %s. "
                "Откройте своего бота в Telegram и нажмите /start, затем проверьте ADMIN_ID.",
                type(exc).__name__,
                exc.message,
            )
