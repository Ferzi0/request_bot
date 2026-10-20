"""Подсказка «Диалог был прерван», когда бот потерял шаги после перезапуска (этап 2з)."""

from __future__ import annotations

import asyncio
import logging

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage, SendPhoto, TelegramMethod
from aiogram.types import CallbackQuery, InaccessibleMessage, InlineKeyboardMarkup, Update, User

from bot import texts
from bot.app import create_dispatcher
from bot.callbacks import AdminCallback, PanelAction, PanelCallback
from bot.config import Settings
from tests.conftest import ADMIN_ID, CLIENT_ID, FakeUser, MockSession, callback_of
from tests.test_flow import APPLY, SUBMIT, TASK, fill_until_summary

ANNA = User(id=CLIENT_ID, is_bot=False, first_name="Анна", last_name="К", username="anna_k")


def hints(session: MockSession) -> list[SendMessage]:
    return [m for m in session.sent(CLIENT_ID) if m.text == texts.DIALOG_INTERRUPTED]


def restarted(settings: Settings, bot: Bot, session: MockSession) -> FakeUser:
    """Тот же человек, но бот перезапущен: новый диспетчер, пустое хранилище FSM."""
    return FakeUser(create_dispatcher(settings), bot, session, ANNA)


async def old_buttons(client: FakeUser, session: MockSession) -> dict[str, tuple[str, SendMessage]]:
    """Дойти до сводки и запомнить кнопки трёх шагов: тип проекта, согласие, отправка."""
    await client.send("/start")
    await client.press(APPLY)
    await client.send("Анна")
    await client.send("anna@mail.ru")
    question = session.last_sent(CLIENT_ID)
    await client.press(callback_of(question, "Лендинг"))
    await client.send(TASK)
    consent = session.last_sent(CLIENT_ID)
    await client.press(callback_of(consent, texts.BTN_AGREE))
    summary = session.last_sent(CLIENT_ID)
    return {
        "тип": (callback_of(question, "Лендинг"), question),
        "согласие": (callback_of(consent, texts.BTN_AGREE), consent),
        "отправить": (SUBMIT, summary),
    }


@pytest.mark.parametrize("which", ["тип", "согласие", "отправить"])
async def test_old_step_button_after_restart_gets_hint(
    client: FakeUser, settings: Settings, bot: Bot, session: MockSession, which: str
) -> None:
    buttons = await old_buttons(client, session)
    after = restarted(settings, bot, session)
    session.clear()
    data, message = buttons[which]
    await after.press(data, on=message)

    assert session.answers()[-1].text == texts.STALE_BUTTON  # всплывашка осталась
    assert len(session.markup_edits()) == 1  # старые кнопки сняты, как и раньше
    (hint,) = hints(session)
    assert isinstance(hint.reply_markup, InlineKeyboardMarkup)
    (button,) = [b for row in hint.reply_markup.inline_keyboard for b in row]
    assert (button.text, button.style) == (texts.BTN_APPLY, "success")
    assert not [
        m for m in session.requests if isinstance(m, SendMessage) and int(m.chat_id) == ADMIN_ID
    ]


async def test_hint_is_not_repeated_on_fast_taps(
    client: FakeUser, settings: Settings, bot: Bot, session: MockSession
) -> None:
    buttons = await old_buttons(client, session)
    after = restarted(settings, bot, session)
    session.clear()
    await asyncio.gather(*(after.press(data, on=msg) for data, msg in buttons.values()))
    assert len(hints(session)) == 1
    assert [a.text for a in session.answers()] == [texts.STALE_BUTTON] * 3


async def test_hint_button_starts_dialog_again(
    client: FakeUser, settings: Settings, bot: Bot, session: MockSession
) -> None:
    buttons = await old_buttons(client, session)
    after = restarted(settings, bot, session)
    await after.press(*buttons["тип"][:1], on=buttons["тип"][1])
    await after.press(APPLY)  # кнопка «Оставить заявку» под подсказкой
    assert session.last_sent(CLIENT_ID).text == texts.ASK_NAME
    await after.send("Анна")
    assert session.last_sent(CLIENT_ID).text == texts.ASK_CONTACT


async def test_stale_button_with_live_state_gets_only_popup(
    client: FakeUser, session: MockSession
) -> None:
    """Двойной тап / старый экран при живом диалоге — как раньше, без подсказки."""
    buttons = await old_buttons(client, session)
    session.clear()
    await client.press(buttons["тип"][0], on=buttons["тип"][1])  # шаг уже пройден
    assert session.answers()[-1].text == texts.STALE_BUTTON
    assert hints(session) == []


async def test_after_submit_old_button_gets_only_popup(
    client: FakeUser, session: MockSession
) -> None:
    """Заявка отправлена (шаг сброшен, но бот человека помнит) — это не «прерванный диалог»."""
    await fill_until_summary(client, session)
    summary = session.last_sent(CLIENT_ID)
    await client.press(SUBMIT, on=summary)
    session.clear()
    await client.press(SUBMIT, on=summary)
    assert session.answers()[-1].text == texts.STALE_BUTTON
    assert hints(session) == []


async def test_stateless_buttons_still_work_after_restart(
    client: FakeUser, settings: Settings, bot: Bot, session: MockSession
) -> None:
    """Приветствие, панель и «В работу» работают без состояния — подсказки нет."""
    await fill_until_summary(client, session)
    await client.press(SUBMIT)
    notification = session.last_sent(ADMIN_ID)

    dp = create_dispatcher(settings)  # «перезапуск»
    after_client = FakeUser(dp, bot, session, ANNA)
    admin = FakeUser(dp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Эдуард"))
    session.clear()
    await after_client.press(APPLY)  # «Оставить заявку» со старого приветствия
    assert session.last_sent(CLIENT_ID).text == texts.ASK_NAME
    take = [b.callback_data for row in notification.reply_markup.inline_keyboard for b in row][-1]
    await admin.press(take or "", on=notification)  # «В работу» под уведомлением
    assert "✅ <b>В работе</b> с " in session.edits()[-1].text
    await admin.press(PanelCallback(a=PanelAction.HOME).pack())  # кнопка панели
    assert session.edits()[-1].text == texts.PANEL_DB_UNAVAILABLE
    assert hints(session) == []
    assert AdminCallback.__prefix__ == "adm"  # префиксы админа не в списке «диалоговых»


async def test_unknown_or_admin_like_data_gets_no_hint(
    settings: Settings, bot: Bot, session: MockSession
) -> None:
    dp: Dispatcher = create_dispatcher(settings)
    admin = FakeUser(dp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    stranger = FakeUser(dp, bot, session, ANNA)
    for user in (admin, stranger):
        await user.press("p:zz")  # испорченная кнопка панели
        await user.press("что-то совсем другое")
    assert hints(session) == []
    assert session.sent(ADMIN_ID) == []


async def test_blocked_client_hint_failure_is_quiet(
    client: FakeUser,
    settings: Settings,
    bot: Bot,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    buttons = await old_buttons(client, session)

    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, SendMessage | SendPhoto) and int(method.chat_id) == CLIENT_ID:
            raise TelegramForbiddenError(method, "Forbidden: bot was blocked by Анна")
        return None

    session.hook = hook
    caplog.set_level(logging.INFO)
    await restarted(settings, bot, session).press(*buttons["тип"][:1], on=buttons["тип"][1])
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "Подсказка о прерванном диалоге не отправлена: TelegramForbiddenError" in caplog.text
    assert "Анна" not in caplog.text
    assert texts.UNEXPECTED_ERROR not in [m.text for m in session.sent(CLIENT_ID)]


async def test_group_gets_nothing(settings: Settings, bot: Bot, session: MockSession) -> None:
    member = FakeUser(
        create_dispatcher(settings), bot, session, ANNA, chat_type="supergroup", chat_id=-100123
    )
    await member.press(SUBMIT)
    assert session.requests == []


async def test_old_button_on_inaccessible_message_does_not_fail(
    settings: Settings, bot: Bot, session: MockSession
) -> None:
    """Telegram не отдал сообщение с кнопкой (старое или удалённое): чат известен."""
    after = restarted(settings, bot, session)
    callback = CallbackQuery(
        id="x",
        from_user=ANNA,
        chat_instance="ci",
        message=InaccessibleMessage(chat=after.chat, message_id=5),
        data=SUBMIT,
    )
    await after.feed(Update(update_id=10**6, callback_query=callback))
    assert session.answers()[-1].text == texts.STALE_BUTTON
    assert session.edits() == []  # снимать кнопки не с чего
    assert len(hints(session)) == 1
    assert texts.UNEXPECTED_ERROR not in [m.text for m in session.sent(CLIENT_ID)]
