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
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from bot import texts
from bot.config import Settings
from bot.handlers import get_routers
from bot.middlewares import PrivateChatOnlyMiddleware
from bot.services.client_notifier import ClientNotifier
from bot.services.formatting import format_database_status
from bot.services.media import PhotoSender
from bot.services.notifier import AdminNotifier, Sleep
from bot.services.panel_state import PanelMessages
from bot.services.rate_limit import SlidingWindowLimiter
from bot.services.storage import ApplicationRepository, DatabaseStatus, NullRepository
from bot.services.storage_alerts import StorageAlerts
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
    database: DatabaseStatus | None = None,
    sleep: Sleep | None = None,
) -> Dispatcher:
    """database — состояние базы при старте для сообщения администратору (None — не сообщать)."""
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
    dp["database"] = database
    dp["storage_alerts"] = StorageAlerts(settings.admin_id)
    media = PhotoSender()  # один на бота: file_id картинок загружаются один раз
    dp["media"] = media
    dp["client_notifier"] = ClientNotifier(settings.admin_id, media=media)
    dp["panel_messages"] = PanelMessages()

    private_only = PrivateChatOnlyMiddleware()
    dp.message.outer_middleware(private_only)
    dp.callback_query.outer_middleware(private_only)

    dp.include_routers(*get_routers())
    dp.startup.register(on_startup)
    return dp


# Вызовы при старте — не критичны, поэтому ждём ответа недолго: по умолчанию aiogram ждёт 60 с,
# и при недоступном Telegram старт растянулся бы на минуты (заявки при этом не теряются).
STARTUP_REQUEST_TIMEOUT = 10


async def on_startup(bot: Bot, settings: Settings, database: DatabaseStatus | None) -> None:
    """Меню команд и проверочное сообщение администратору. Ошибки здесь не фатальны."""
    common = [
        BotCommand(command="start", description=texts.CMD_START),
        BotCommand(command="help", description=texts.CMD_HELP),
        BotCommand(command="cancel", description=texts.CMD_CANCEL),
    ]
    try:
        await bot.set_my_commands(
            common,
            scope=BotCommandScopeAllPrivateChats(),
            request_timeout=STARTUP_REQUEST_TIMEOUT,
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось установить меню команд: %s", type(exc).__name__)
    # Меню администратора — отдельным scope на его чат: Telegram показывает список
    # самого узкого scope, поэтому общие команды повторяем, а «Админ-панель»
    # видит только он. Если администратор ещё не писал боту, Telegram откажет —
    # это не мешает работе: /admin работает и без пункта в меню.
    try:
        await bot.set_my_commands(
            [*common, BotCommand(command="admin", description=texts.CMD_ADMIN)],
            scope=BotCommandScopeChat(chat_id=settings.admin_id),
            request_timeout=STARTUP_REQUEST_TIMEOUT,
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось установить меню администратора: %s", type(exc).__name__)

    # Описание бота — из кода, чтобы текст жил в git рядом с остальными, а не только
    # в @BotFather. Telegram принимает повтор того же текста спокойно, поэтому ставим
    # при каждом старте и ничего не храним. Без language_code — для всех языков.
    # Имя бота (set_my_name) не трогаем: его выбрал заказчик.
    try:
        await bot.set_my_description(texts.BOT_DESCRIPTION, request_timeout=STARTUP_REQUEST_TIMEOUT)
    except TelegramAPIError as exc:
        logger.warning("Не удалось установить описание бота: %s", type(exc).__name__)
    try:
        await bot.set_my_short_description(
            texts.BOT_SHORT_DESCRIPTION, request_timeout=STARTUP_REQUEST_TIMEOUT
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось установить короткое описание бота: %s", type(exc).__name__)

    # Пробное сообщение — по настройке NOTIFY_ADMIN_ON_START, а о недоступной базе
    # сообщаем всегда: иначе заказчик не узнает, что заявки перестали сохраняться.
    db_line = format_database_status(database) if database is not None else None
    if settings.notify_admin_on_start:
        text: str | None = "\n\n".join(filter(None, (texts.ADMIN_STARTED, db_line)))
    elif database is not None and not database.ok:
        text = db_line
    else:
        text = None
    if text:
        try:
            await bot.send_message(settings.admin_id, text)
        except TelegramAPIError as exc:
            # Частая причина: администратор ещё не открыл бота и не нажал /start —
            # бот не может написать первым. Тогда заявки тоже не дойдут.
            logger.error(
                "Не удалось написать администратору (ADMIN_ID): %s: %s. "
                "Откройте своего бота в Telegram и нажмите /start, затем проверьте ADMIN_ID.",
                type(exc).__name__,
                exc.message,
            )
