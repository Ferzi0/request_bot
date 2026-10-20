"""Одна актуальная панель: повторный /admin удаляет прежнюю (этап 2г)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import Chat, InaccessibleMessage, Message, User

from bot import texts
from bot.callbacks import PanelAction, PanelCallback
from bot.config import Settings
from bot.services.panel_state import PanelMessages
from bot.services.sqlite_repository import SqliteRepository
from bot.services.storage import NullRepository
from tests.conftest import ADMIN_ID, BOT_ID, FakeUser, MockSession
from tests.test_flow import APPLY
from tests.test_panel import make_dp
from tests.test_storage import make_app

STRANGER_ID = 4242
HOME = PanelCallback(a=PanelAction.HOME).pack()
LIST_ALL = PanelCallback(a=PanelAction.LIST).pack()


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[SqliteRepository]:
    async with SqliteRepository(tmp_path / "bot.db") as repository:
        await repository.add(make_app("261020-0001"))
        yield repository


@pytest.fixture
def pdp(settings: Settings, repo: SqliteRepository) -> Dispatcher:
    return make_dp(settings, repo)


@pytest.fixture
def admin(pdp: Dispatcher, bot: Bot, session: MockSession) -> FakeUser:
    return FakeUser(pdp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Эдуард"))


def remembered(dp: Dispatcher) -> int | None:
    panel_messages: PanelMessages = dp["panel_messages"]
    return panel_messages.get(ADMIN_ID)


def deletes(session: MockSession) -> list[tuple[int, int]]:
    return [
        (int(m.chat_id), m.message_id) for m in session.requests if isinstance(m, DeleteMessage)
    ]


def panels(session: MockSession) -> list[SendMessage]:
    return [m for m in session.sent(ADMIN_ID) if m.text == texts.PANEL_HOME]


def panel_message(session: MockSession, sent: SendMessage) -> Message:
    """Сообщение панели так, как его пришлёт Telegram в callback (с настоящим id)."""
    return Message(
        message_id=session.message_ids[id(sent)],
        date=datetime.now(UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"),
        text=sent.text,
        reply_markup=sent.reply_markup,
    )


def last_command_id(admin: FakeUser) -> int:
    # FakeUser нумерует свои сообщения с 10 000 подряд: id последней команды.
    return next(admin._msg_ids) - 1


# ---------- /admin ----------


def test_panel_messages_store() -> None:
    store = PanelMessages()
    assert store.get(ADMIN_ID) is None
    store.set(ADMIN_ID, 7)
    store.set(ADMIN_ID, 9)
    assert store.get(ADMIN_ID) == 9 and store.get(1) is None


async def test_first_admin_only_sends(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/admin")
    command_id = last_command_id(admin)
    (panel,) = panels(session)
    assert deletes(session) == [(ADMIN_ID, command_id)]  # старой нет — удаляем только команду
    assert remembered(pdp) == session.message_ids[id(panel)]


async def test_second_admin_replaces_panel(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/admin")
    old_id = remembered(pdp)
    session.clear()
    await admin.send("/admin")
    command_id = last_command_id(admin)
    (panel,) = panels(session)
    new_id = session.message_ids[id(panel)]
    # Порядок: удалить старую панель, удалить команду, прислать новую.
    kinds = [type(m).__name__ for m in session.requests]
    assert kinds == ["DeleteMessage", "DeleteMessage", "SendMessage"]
    assert deletes(session) == [(ADMIN_ID, old_id), (ADMIN_ID, command_id)]
    assert remembered(pdp) == new_id != old_id


async def test_navigation_keeps_id(admin: FakeUser, pdp: Dispatcher, session: MockSession) -> None:
    await admin.send("/admin")
    panel_id = remembered(pdp)
    for data in (
        LIST_ALL,
        PanelCallback(a=PanelAction.CARD, i="261020-0001").pack(),
        PanelCallback(a=PanelAction.ASK_CANCEL, i="261020-0001").pack(),
        HOME,
    ):
        await admin.press(data)
        assert remembered(pdp) == panel_id
    assert len(session.edits()) == 4 and len(panels(session)) == 1


async def test_fallback_new_message_updates_id(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/admin")
    old_id = remembered(pdp)

    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method, "Bad Request: message to edit not found")
        return None

    session.hook = hook
    await admin.press(LIST_ALL)
    screen = session.last_sent(ADMIN_ID)
    assert screen.text.startswith("<b>📋 Все заявки</b>")
    assert remembered(pdp) == session.message_ids[id(screen)] != old_id

    # Следующий /admin удаляет именно новое сообщение
    session.hook = None
    session.clear()
    await admin.send("/admin")
    assert deletes(session)[0] == (ADMIN_ID, session.message_ids[id(screen)])


async def test_inaccessible_message_updates_id(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/admin")
    gone = InaccessibleMessage(chat=Chat(id=ADMIN_ID, type="private"), message_id=remembered(pdp))
    await admin.press(HOME, message=gone)  # type: ignore[arg-type]
    assert remembered(pdp) == session.message_ids[id(session.last_sent(ADMIN_ID))]


@pytest.mark.parametrize(
    "error",
    [
        lambda m: TelegramBadRequest(m, "Bad Request: message to delete not found"),
        lambda m: TelegramBadRequest(m, "Bad Request: message can't be deleted"),
        lambda m: TelegramForbiddenError(m, "Forbidden: Эдуард"),
        lambda m: TelegramNetworkError(m, "timeout Эдуард"),
    ],
)
async def test_delete_failure_still_opens_panel(
    admin: FakeUser,
    pdp: Dispatcher,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
    error,  # type: ignore[no-untyped-def]
) -> None:
    caplog.set_level(logging.INFO)
    await admin.send("/admin")

    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, DeleteMessage):
            raise error(method)
        return None

    session.hook = hook
    session.clear()
    await admin.send("/admin")
    (panel,) = panels(session)
    assert remembered(pdp) == session.message_ids[id(panel)]
    assert texts.UNEXPECTED_ERROR not in [m.text for m in session.sent(ADMIN_ID)]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "не удалось удалить (старая панель)" in caplog.text
    assert "Эдуард" not in caplog.text  # текст ошибки в лог не попадает, только класс


async def test_stranger_admin_deletes_nothing(
    pdp: Dispatcher, bot: Bot, session: MockSession
) -> None:
    stranger = FakeUser(pdp, bot, session, User(id=STRANGER_ID, is_bot=False, first_name="Г"))
    await stranger.send("/admin")
    await stranger.send("/admin")
    assert deletes(session) == []
    assert [m.text for m in session.sent(STRANGER_ID)] == [texts.UNKNOWN_COMMAND] * 2
    assert remembered(pdp) is None and pdp["panel_messages"].get(STRANGER_ID) is None


async def test_deletes_only_in_admin_chat(
    admin: FakeUser, pdp: Dispatcher, bot: Bot, session: MockSession
) -> None:
    stranger = FakeUser(pdp, bot, session, User(id=STRANGER_ID, is_bot=False, first_name="Г"))
    await admin.send("/admin")
    await stranger.send("/admin")
    await stranger.press(HOME)  # посторонний жмёт кнопку панели
    await admin.send("/admin")
    assert deletes(session) and all(chat == ADMIN_ID for chat, _ in deletes(session))


async def test_admin_mid_application_keeps_step(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/start")
    await admin.press(APPLY)  # администратор сам заполняет заявку: шаг «имя»
    await admin.send("/admin")
    await admin.send("/admin")
    assert len(panels(session)) == 2
    await admin.send("Эдуард")
    assert session.last_sent(ADMIN_ID).text == texts.ASK_CONTACT  # шаг не сброшен


async def test_replaced_panel_buttons_are_stale(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/admin")
    old_panel = panels(session)[0]
    await admin.send("/admin")
    new_id = remembered(pdp)
    session.clear()
    # Нажали кнопку старой панели (до её удаления или из кэша клиента)
    await admin.press(LIST_ALL, message=panel_message(session, old_panel))
    assert session.answers()[-1].text == texts.STALE_BUTTON
    assert session.edits() == [] and session.sent(ADMIN_ID) == []
    assert deletes(session) == [(ADMIN_ID, session.message_ids[id(old_panel)])]
    assert remembered(pdp) == new_id


async def test_after_restart_any_panel_works_and_becomes_current(
    settings: Settings, repo: SqliteRepository, bot: Bot, session: MockSession
) -> None:
    before = make_dp(settings, repo)
    admin = FakeUser(before, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    await admin.send("/admin")
    old_panel = panels(session)[0]

    after = make_dp(settings, repo)  # «перезапуск»: бот ничего не помнит
    admin_after = FakeUser(after, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    await admin_after.press(LIST_ALL, message=panel_message(session, old_panel))
    assert session.edits()[-1].text.startswith("<b>📋 Все заявки</b>")
    assert remembered(after) == session.message_ids[id(old_panel)]


# ---------- гонки (антибаг 2г) ----------
# Апдейты одного администратора идут по очереди (SimpleEventIsolation в
# create_dispatcher). Без неё два /admin оставили бы «сироту», а нажатие старой
# панели во время /admin вернуло бы в память id уже удалённой панели.


async def test_two_fast_admin_commands_leave_one_panel(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/admin")
    session.slow_chat = ADMIN_ID  # ответы медленные — апдейты пересекаются во времени
    await asyncio.gather(admin.send("/admin"), admin.send("/admin"))
    session.slow_chat = None
    *replaced, current = (session.message_ids[id(p)] for p in panels(session))
    assert len(replaced) == 2
    deleted = {message_id for _, message_id in deletes(session)}
    assert set(replaced) <= deleted and current not in deleted  # одна панель в чате
    assert remembered(pdp) == current


async def test_old_panel_press_during_admin(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    await admin.send("/admin")
    old_panel = panels(session)[0]
    old_id = session.message_ids[id(old_panel)]
    session.clear()
    session.slow_chat = ADMIN_ID
    await asyncio.gather(
        admin.send("/admin"), admin.press(LIST_ALL, message=panel_message(session, old_panel))
    )
    session.slow_chat = None
    current = session.message_ids[id(panels(session)[-1])]
    assert remembered(pdp) == current  # не id удалённой старой панели
    assert (ADMIN_ID, old_id) in deletes(session) and (ADMIN_ID, current) not in deletes(session)
    # Старую панель после её удаления никто не правит (иначе — ещё одна панель в чате).
    kinds = [
        type(m).__name__
        for m in session.requests
        if isinstance(m, DeleteMessage | EditMessageText) and m.message_id == old_id
    ]
    assert "EditMessageText" not in kinds[kinds.index("DeleteMessage") :]


async def test_send_failure_after_delete_does_not_break_next_admin(
    admin: FakeUser, pdp: Dispatcher, session: MockSession
) -> None:
    # Старую панель удалили, а новую отправить не удалось (сеть): в памяти остаётся
    # id удалённой — следующий /admin тихо пробует удалить её снова и открывает панель.
    await admin.send("/admin")
    old_id = remembered(pdp)

    def no_network(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, SendMessage) and method.text == texts.PANEL_HOME:
            raise TelegramNetworkError(method, "timeout")
        return None

    session.hook = no_network
    await admin.send("/admin")
    assert remembered(pdp) == old_id  # новая не ушла — в памяти id уже удалённой

    def already_deleted(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, DeleteMessage) and method.message_id == old_id:
            raise TelegramBadRequest(method, "Bad Request: message to delete not found")
        return None

    session.hook = already_deleted
    session.clear()
    await admin.send("/admin")
    (panel,) = panels(session)
    assert remembered(pdp) == session.message_ids[id(panel)] != old_id


async def test_foreign_private_chat_deletes_nothing(
    pdp: Dispatcher, bot: Bot, session: MockSession
) -> None:
    # Защита на всякий случай: middleware пускает только личные чаты, где chat.id
    # равен id пользователя, но удаление и память всё равно привязаны к ADMIN_ID.
    user = User(id=ADMIN_ID, is_bot=False, first_name="Э")
    elsewhere = FakeUser(pdp, bot, session, user, chat_id=STRANGER_ID)
    await elsewhere.send("/admin")
    await elsewhere.send("/admin")
    assert deletes(session) == []
    assert pdp["panel_messages"].get(STRANGER_ID) is None and remembered(pdp) is None


async def test_after_restart_same_screen_still_adopts_panel(
    settings: Settings, repo: SqliteRepository, bot: Bot, session: MockSession
) -> None:
    before = make_dp(settings, repo)
    admin = FakeUser(before, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    await admin.send("/admin")
    old_panel = panels(session)[0]

    def not_modified(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method, "Bad Request: message is not modified")
        return None

    session.hook = not_modified
    after = make_dp(settings, repo)  # «перезапуск»; нажата «В панель» на главном экране
    admin_after = FakeUser(after, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    await admin_after.press(HOME, message=panel_message(session, old_panel))
    assert remembered(after) == session.message_ids[id(old_panel)]


async def test_unavailable_database_panel_is_replaced_too(
    settings: Settings, bot: Bot, session: MockSession
) -> None:
    # Без базы панель пишет «база недоступна» — это тоже панель: её id запоминается,
    # и повторный /admin удаляет её так же.
    dp = make_dp(settings, NullRepository())
    admin = FakeUser(dp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    await admin.send("/admin")
    first = session.last_sent(ADMIN_ID)
    assert first.text == texts.PANEL_DB_UNAVAILABLE
    await admin.send("/admin")
    assert (ADMIN_ID, session.message_ids[id(first)]) in deletes(session)
    assert remembered(dp) == session.message_ids[id(session.last_sent(ADMIN_ID))]
