"""Описание бота в профиле Telegram — ставится из кода при старте (этап 2ж)."""

from __future__ import annotations

import logging

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import (
    SendMessage,
    SetMyCommands,
    SetMyDescription,
    SetMyName,
    SetMyShortDescription,
    TelegramMethod,
)

from bot import texts
from bot.app import STARTUP_REQUEST_TIMEOUT, create_dispatcher
from bot.config import Settings
from tests.conftest import ADMIN_ID, MockSession

DESCRIPTION_LIMIT = 512  # лимиты Bot API: setMyDescription / setMyShortDescription
SHORT_DESCRIPTION_LIMIT = 120


async def start(settings: Settings, bot: Bot) -> None:
    dp = create_dispatcher(settings)
    await dp.emit_startup(bot=bot, **dp.workflow_data)


async def test_description_is_set_on_start(
    settings: Settings, bot: Bot, session: MockSession
) -> None:
    await start(settings, bot)
    (description,) = [m for m in session.requests if isinstance(m, SetMyDescription)]
    (short,) = [m for m in session.requests if isinstance(m, SetMyShortDescription)]
    assert description.description == texts.BOT_DESCRIPTION
    assert short.short_description == texts.BOT_SHORT_DESCRIPTION
    # Для всех языков, имя бота не трогаем
    assert description.language_code is None and short.language_code is None
    assert not [m for m in session.requests if isinstance(m, SetMyName)]


async def test_startup_calls_use_short_timeout(
    settings: Settings, bot: Bot, session: MockSession
) -> None:
    """Меню и описание не должны растягивать старт на 60 с при недоступном Telegram."""
    await start(settings, bot)
    profile = (SetMyCommands, SetMyDescription, SetMyShortDescription)
    timeouts = [t for m, t in session.timeouts if isinstance(m, profile)]
    assert len(timeouts) == 4  # два меню (общее и админа) + два описания
    assert all(t == STARTUP_REQUEST_TIMEOUT for t in timeouts)


@pytest.mark.parametrize(
    "error",
    [
        lambda m: TelegramNetworkError(m, "timeout"),
        lambda m: TelegramBadRequest(m, "Bad Request: description is too long"),
        lambda m: TelegramRetryAfter(m, "Too Many Requests", 30),
    ],
)
@pytest.mark.parametrize("failing", [SetMyDescription, SetMyShortDescription])
async def test_description_failure_does_not_break_start(
    settings: Settings,
    bot: Bot,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
    failing: type,
    error,  # type: ignore[no-untyped-def]
) -> None:
    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, failing):
            raise error(method)
        return None

    session.hook = hook
    notify = settings.model_copy(update={"notify_admin_on_start": True})
    await start(notify, bot)
    # Второй вызов описания выполнен, меню и пробное сообщение — тоже
    assert [m for m in session.requests if isinstance(m, SetMyDescription)]
    assert [m for m in session.requests if isinstance(m, SetMyShortDescription)]
    assert len([m for m in session.requests if isinstance(m, SetMyCommands)]) == 2
    assert [m.text for m in session.sent(ADMIN_ID)] == [texts.ADMIN_STARTED]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "описание бота" in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_description_lengths() -> None:
    assert 0 < len(texts.BOT_DESCRIPTION) <= DESCRIPTION_LIMIT
    assert 0 < len(texts.BOT_SHORT_DESCRIPTION) <= SHORT_DESCRIPTION_LIMIT


def test_description_has_no_prices_or_made_up_terms() -> None:
    for text in (texts.BOT_DESCRIPTION, texts.BOT_SHORT_DESCRIPTION):
        lowered = text.lower()
        assert "₽" not in text and "руб" not in lowered
        assert not any(ch.isdigit() for ch in text)  # ни цен, ни сроков цифрами
        assert "в течение двух часов" in lowered  # единственный срок — с сайта
        assert "<" not in text  # описание Telegram показывает как есть, без HTML


async def test_start_does_not_send_extra_messages(
    settings: Settings, bot: Bot, session: MockSession
) -> None:
    await start(settings, bot)  # notify_admin_on_start=False в тестовых настройках
    assert not [m for m in session.requests if isinstance(m, SendMessage)]
