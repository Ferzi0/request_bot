"""Админ-панель: /admin — заявки по статусам, карточка, смена статуса.

Панель видит только администратор (ADMIN_ID). Для остальных /admin выглядит
как любая неизвестная команда — существование панели не раскрываем.
Все экраны — правка одного и того же сообщения, чтобы не засорять чат.
Повторный /admin удаляет прежнюю панель и саму команду: в чате остаётся одна
актуальная панель (её message_id помнит PanelMessages, в памяти).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot import texts
from bot.callbacks import LIST_ALL, PanelAction, PanelCallback
from bot.config import Settings
from bot.keyboards import (
    panel_card_keyboard,
    panel_confirm_cancel_keyboard,
    panel_home_keyboard,
    panel_home_only_keyboard,
    panel_list_keyboard,
)
from bot.models import ALLOWED_TRANSITIONS, ApplicationRecord, ApplicationStatus
from bot.services.client_notifier import ClientNotifier
from bot.services.formatting import (
    admin_reply_url,
    format_panel_card,
    format_panel_confirm_cancel,
    format_panel_list,
)
from bot.services.panel_state import PanelMessages
from bot.services.storage import ApplicationRepository, StatusOutcome

logger = logging.getLogger(__name__)

PAGE_SIZE = 5
_NOT_MODIFIED = "message is not modified"
_PRIVACY_RESTRICTED = "BUTTON_USER_PRIVACY_RESTRICTED"
# Ответы Telegram, когда сообщение больше нельзя править (удалено, недоступно).
_CANT_EDIT = ("message can't be edited", "message to edit not found")

Screen = tuple[str, InlineKeyboardMarkup]


class _StorageFailedError(Exception):
    """Чтение базы не удалось — экран с честным сообщением вместо списка."""


# ---------- вход ----------


async def cmd_admin(
    message: Message,
    bot: Bot,
    settings: Settings,
    repository: ApplicationRepository,
    panel_messages: PanelMessages,
) -> None:
    if message.from_user is None or message.from_user.id != settings.admin_id:
        # Ровно тот же ответ, что на любую неизвестную команду: панель не «светится».
        await message.answer(texts.UNKNOWN_COMMAND)
        return
    chat_id = message.chat.id
    # Удаляем только в личном чате администратора (middleware пропускает лишь личные
    # чаты, а там chat.id = user id) — сообщения чужих чатов не трогаем никогда.
    if chat_id == settings.admin_id:
        if (old := panel_messages.get(chat_id)) is not None:
            await _delete_quietly(bot, chat_id, old, "старая панель")
        await _delete_quietly(bot, chat_id, message.message_id, "команда /admin")
    text, markup = await _home_screen(repository)
    sent = await message.answer(text, reply_markup=markup)
    _remember(panel_messages, settings, chat_id, sent.message_id)


async def cb_panel(
    callback: CallbackQuery,
    callback_data: PanelCallback,
    bot: Bot,
    settings: Settings,
    repository: ApplicationRepository,
    client_notifier: ClientNotifier,
    panel_messages: PanelMessages,
) -> None:
    # Права — по нажавшему, а не по чату: кнопку мог нажать кто угодно, кому
    # попало это сообщение. Посторонний получает нейтральный ответ, сообщение
    # не меняется. Проверка одна на все кнопки панели — её нельзя забыть.
    if callback.from_user.id != settings.admin_id:
        # %r: значения из callback_data пришли от постороннего — перевод строки
        # в них не должен подделать строку лога.
        logger.warning(
            "panel: отказ в доступе user=%s action=%r app=%r",
            callback.from_user.id,
            callback_data.a,
            callback_data.i,
        )
        await callback.answer(texts.STALE_BUTTON)
        return

    if _is_replaced_panel(callback, panel_messages):
        # Кнопка панели, которую уже заменил повторный /admin (нажата до удаления или
        # из кэша клиента): не оживляем её, а убираем — актуальна только новая.
        await callback.answer(texts.STALE_BUTTON)
        if callback.message is not None:
            chat_id, message_id = callback.message.chat.id, callback.message.message_id
            await _delete_quietly(bot, chat_id, message_id, "заменённая панель")
        return

    data = callback_data
    popup: str | None = None
    newly_taken = False
    match data.a:
        case PanelAction.HOME:
            screen = await _home_screen(repository)
        case PanelAction.LIST:
            screen = await _list_screen(repository, settings, data.f, data.p)
        case PanelAction.CARD:
            screen = await _card_screen(repository, settings, data.i, data.f, data.p)
        case PanelAction.ASK_CANCEL:
            screen = await _confirm_cancel_screen(repository, settings, data)
        case PanelAction.SET:
            popup, screen, newly_taken = await _set_status(repository, settings, data)
    await callback.answer(popup)
    shown_id = await _show(bot, callback, screen)
    if shown_id is not None and callback.message is not None:
        # Правка — тот же id; экран новым сообщением (старое не правится) — новый id.
        _remember(panel_messages, settings, callback.message.chat.id, shown_id)
    if newly_taken:
        # Последним: сбой отправки клиенту не должен мешать экрану и ответу на нажатие.
        await client_notifier.application_taken(bot, repository, data.i)


# ---------- экраны ----------


async def _home_screen(repository: ApplicationRepository) -> Screen:
    if not repository.available:
        return texts.PANEL_DB_UNAVAILABLE, panel_home_only_keyboard()
    try:
        counts = await repository.count_by_status()
    except Exception as exc:
        return _storage_error_screen("home", exc)
    return texts.PANEL_HOME, panel_home_keyboard(counts)


async def _list_screen(
    repository: ApplicationRepository, settings: Settings, list_filter: str, page: int
) -> Screen:
    if not repository.available:
        return texts.PANEL_DB_UNAVAILABLE, panel_home_only_keyboard()
    status = _parse_filter(list_filter)
    title = texts.PANEL_LIST_TITLES[status.value if status else LIST_ALL]
    filter_code = status.value if status else LIST_ALL
    try:
        counts = await repository.count_by_status()
        total = counts[status] if status else sum(counts.values())
        pages = max(1, math.ceil(total / PAGE_SIZE))
        # Страница могла «уехать»: пока список был открыт, заявки сменили статус.
        page = min(max(page, 0), pages - 1)
        records = await repository.list_by_status(status, PAGE_SIZE, page * PAGE_SIZE)
    except Exception as exc:
        return _storage_error_screen("list", exc)
    text = format_panel_list(records, title, total=total, page=page, pages=pages, tz=settings.tz)
    markup = panel_list_keyboard(records, filter_code, page, has_next=page + 1 < pages)
    return text, markup


async def _card_screen(
    repository: ApplicationRepository,
    settings: Settings,
    app_id: str,
    list_filter: str,
    page: int,
) -> Screen:
    if not repository.available:
        return texts.PANEL_DB_UNAVAILABLE, panel_home_only_keyboard()
    try:
        record = await repository.get(app_id)
    except Exception as exc:
        return _storage_error_screen("card", exc)
    if record is None:
        return _not_found_screen(app_id)
    return _card(record, settings, list_filter, page)


async def _confirm_cancel_screen(
    repository: ApplicationRepository, settings: Settings, data: PanelCallback
) -> Screen:
    if not repository.available:
        return texts.PANEL_DB_UNAVAILABLE, panel_home_only_keyboard()
    try:
        record = await repository.get(data.i)
    except Exception as exc:
        return _storage_error_screen("confirm", exc)
    if record is None:
        return _not_found_screen(data.i)
    if ApplicationStatus.CANCELLED not in ALLOWED_TRANSITIONS[record.status]:
        # Старая кнопка: заявку уже закрыли — показываем актуальную карточку.
        return _card(record, settings, data.f, data.p)
    return (
        format_panel_confirm_cancel(record, settings.tz),
        panel_confirm_cancel_keyboard(record.application.id, data.f, data.p),
    )


async def _set_status(
    repository: ApplicationRepository, settings: Settings, data: PanelCallback
) -> tuple[str | None, Screen, bool]:
    """Смена статуса. Третий элемент — заявку только что взяли в работу (new → in_work)."""
    if not repository.available:
        return None, (texts.PANEL_DB_UNAVAILABLE, panel_home_only_keyboard()), False
    try:
        target = ApplicationStatus(data.t)
    except ValueError:
        logger.warning("panel: неизвестный статус %r", data.t)
        screen = await _card_screen(repository, settings, data.i, data.f, data.p)
        return texts.STALE_BUTTON, screen, False
    try:
        change = await repository.set_status(data.i, target, datetime.now(UTC))
    except Exception as exc:
        error = type(exc).__name__
        logger.error("panel: заявка %r — статус %s не записан: %s", data.i, target.value, error)
        screen = await _card_screen(repository, settings, data.i, data.f, data.p)
        return texts.PANEL_STATUS_FAILED.format(error=error), screen, False

    record = change.record
    if change.outcome is StatusOutcome.NOT_FOUND or record is None:
        return None, _not_found_screen(data.i), False
    match change.outcome:
        case StatusOutcome.CHANGED:
            logger.info("panel: заявка %s → %s", record.application.id, record.status.value)
            popup = texts.PANEL_STATUS_CHANGED.format(label=record.status.label)
        case StatusOutcome.ALREADY:
            popup = texts.PANEL_STATUS_ALREADY.format(label=record.status.label)
        case _:
            popup = texts.PANEL_STATUS_NOT_ALLOWED.format(label=record.status.label)
    newly_taken = (
        change.outcome is StatusOutcome.CHANGED and record.status is ApplicationStatus.IN_WORK
    )
    return popup, _card(record, settings, data.f, data.p), newly_taken


# ---------- помощники ----------


def _card(record: ApplicationRecord, settings: Settings, list_filter: str, page: int) -> Screen:
    reply_url = admin_reply_url(record.application)
    return (
        format_panel_card(record, settings.tz),
        panel_card_keyboard(record, reply_url, list_filter, page),
    )


def _parse_filter(value: str) -> ApplicationStatus | None:
    """Фильтр из callback_data: код статуса или all; всё прочее — «все»."""
    try:
        return ApplicationStatus(value)
    except ValueError:
        return None


def _not_found_screen(app_id: str) -> Screen:
    # app_id пришёл из callback_data: в текст — только через экранирование.
    return texts.PANEL_NOT_FOUND.format(app_id=escape(app_id)), panel_home_only_keyboard()


def _storage_error_screen(where: str, exc: Exception) -> Screen:
    error = type(exc).__name__
    logger.error("panel: ошибка чтения базы (%s): %s", where, error)
    return texts.PANEL_DB_ERROR.format(error=error), panel_home_only_keyboard()


async def _show(bot: Bot, callback: CallbackQuery, screen: Screen) -> int | None:
    """Показывает экран правкой того же сообщения, а если править нельзя — новым.

    Возвращает message_id, где теперь панель (None — сообщения нет совсем).
    """
    text, markup = screen
    message = callback.message
    if not isinstance(message, Message):
        # InaccessibleMessage: Telegram не отдал сообщение (удалено или недоступно
        # боту) — править нечего, шлём экран новым сообщением.
        if message is not None:
            return await _send_new(bot, message.chat.id, screen)
        return None
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if _NOT_MODIFIED in exc.message:
            return message.message_id  # нажали ту же кнопку — экран уже такой
        if _PRIVACY_RESTRICTED in exc.message:
            # Кнопку tg://user Telegram не принимает (приватность клиента) — без неё.
            await message.edit_text(text, reply_markup=_without_tg_links(markup))
            return message.message_id
        if any(reason in exc.message for reason in _CANT_EDIT):
            # Сообщение успели удалить или Telegram отказывается его править:
            # статус к этому моменту уже записан, поэтому экран — новым сообщением,
            # а не «Что-то пошло не так».
            return await _send_new(bot, message.chat.id, screen)
        raise
    return message.message_id


async def _send_new(bot: Bot, chat_id: int, screen: Screen) -> int:
    text, markup = screen
    try:
        sent = await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if _PRIVACY_RESTRICTED not in exc.message:
            raise
        sent = await bot.send_message(chat_id, text, reply_markup=_without_tg_links(markup))
    return sent.message_id


def _remember(panel_messages: PanelMessages, settings: Settings, chat_id: int, msg: int) -> None:
    if chat_id == settings.admin_id:  # только чат администратора
        panel_messages.set(chat_id, msg)


def _is_replaced_panel(callback: CallbackQuery, panel_messages: PanelMessages) -> bool:
    """Кнопка нажата на панели, которая уже не текущая.

    Если бот текущую панель не помнит (после перезапуска), любая панель считается
    рабочей и становится текущей — иначе после рестарта ею нельзя было бы пользоваться.
    """
    if callback.message is None:
        return False
    current = panel_messages.get(callback.message.chat.id)
    return current is not None and callback.message.message_id != current


async def _delete_quietly(bot: Bot, chat_id: int, message_id: int, what: str) -> None:
    """Удаление — уборка, а не суть: любой сбой (уже удалено, нельзя удалить, сеть)
    только в лог (класс ошибки), панель всё равно открывается."""
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as exc:
        logger.info("panel: не удалось удалить (%s): %s", what, type(exc).__name__)


def _without_tg_links(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows = [
        [b for b in row if not (b.url or "").startswith("tg://")] for row in markup.inline_keyboard
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def create_router() -> Router:
    """Админ-панель. Подключать ДО роутера общих команд: там /admin поймал бы
    обработчик неизвестных команд."""
    router = Router(name="panel")
    router.message.register(cmd_admin, Command("admin"))
    router.callback_query.register(cb_panel, PanelCallback.filter())
    return router
