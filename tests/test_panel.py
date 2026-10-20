"""Админ-панель /admin и кнопка «В работу» со статусом в базе — через dp.feed_update.

Все базы — во временных папках pytest; data/ проекта тесты не трогают.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage, SetMyCommands, TelegramMethod
from aiogram.types import (
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    Update,
    User,
)

from bot import texts
from bot.app import create_dispatcher
from bot.callbacks import PanelAction, PanelCallback
from bot.config import Settings
from bot.models import Applicant, ApplicationStatus, ProjectType
from bot.services.formatting import format_local_time
from bot.services.sqlite_repository import SqliteRepository
from bot.services.storage import ApplicationRepository, NullRepository
from bot.services.storage_alerts import StorageAlerts
from tests.conftest import ADMIN_ID, CLIENT_ID, FakeUser, MockSession, as_telegram_sees, buttons
from tests.test_flow import APPLY, SUBMIT, fill_until_summary
from tests.test_storage import make_app

NEW, IN_WORK, DONE, CANCELLED = (
    ApplicationStatus.NEW,
    ApplicationStatus.IN_WORK,
    ApplicationStatus.DONE,
    ApplicationStatus.CANCELLED,
)
T0 = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
STRANGER_ID = 4242


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[SqliteRepository]:
    async with SqliteRepository(tmp_path / "bot.db") as repository:
        yield repository


def make_dp(settings: Settings, repository: object) -> Dispatcher:
    return create_dispatcher(settings, repository=repository)  # type: ignore[arg-type]


@pytest.fixture
def panel_dp(settings: Settings, repo: SqliteRepository) -> Dispatcher:
    return make_dp(settings, repo)


@pytest.fixture
def admin_user(panel_dp: Dispatcher, bot: Bot, session: MockSession) -> FakeUser:
    user = User(id=ADMIN_ID, is_bot=False, first_name="Эдуард", username="ferz_admin")
    return FakeUser(panel_dp, bot, session, user)


@pytest.fixture
def stranger(panel_dp: Dispatcher, bot: Bot, session: MockSession) -> FakeUser:
    return FakeUser(panel_dp, bot, session, User(id=STRANGER_ID, is_bot=False, first_name="Гость"))


async def seed(repo: ApplicationRepository, statuses: list[ApplicationStatus]) -> list[str]:
    """Заявки с нужными статусами; первая в списке — самая свежая."""
    ids = []
    for n, status in enumerate(statuses):
        app_id = f"261019-{n:04X}"
        await repo.add(make_app(app_id, created_at=T0 - timedelta(minutes=n)))
        path = {NEW: [], IN_WORK: [IN_WORK], DONE: [IN_WORK, DONE], CANCELLED: [CANCELLED]}
        for step in path[status]:
            await repo.set_status(app_id, step, T0)
        ids.append(app_id)
    return ids


def screen(session: MockSession) -> tuple[str, InlineKeyboardMarkup]:
    """Текущий экран панели: последняя правка или последнее сообщение администратору."""
    for method in reversed(session.requests):
        if isinstance(method, EditMessageText | SendMessage) and (
            isinstance(method, EditMessageText) or int(method.chat_id) == ADMIN_ID
        ):
            markup = method.reply_markup
            assert isinstance(markup, InlineKeyboardMarkup)
            return method.text, markup
    raise AssertionError("экрана нет")


def labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def data_of(markup: InlineKeyboardMarkup, label: str) -> str:
    for row in markup.inline_keyboard:
        for b in row:
            if b.text == label and b.callback_data:
                return b.callback_data
    raise AssertionError(f"нет кнопки {label!r}: {labels(markup)}")


async def press(user: FakeUser, session: MockSession, label: str) -> None:
    """Нажать кнопку текущего экрана."""
    await user.press(data_of(screen(session)[1], label), on=None)


async def open_list(user: FakeUser, session: MockSession, label: str) -> None:
    await user.send("/admin")
    await press(user, session, label)


# ---------- доступ ----------


async def test_admin_opens_panel_with_counters(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await seed(repo, [NEW, NEW, IN_WORK, DONE, DONE, DONE, CANCELLED])
    await admin_user.send("/admin")
    text, markup = screen(session)
    assert text == texts.PANEL_HOME
    assert labels(markup) == [
        "🆕 Новые (2)",
        "🔧 В работе (1)",
        "✅ Выполненные (3)",
        "🚫 Отменённые (1)",
        "📋 Все",
    ]


async def test_stranger_does_not_see_panel(
    stranger: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await seed(repo, [NEW])
    await stranger.send("/admin")
    (reply,) = session.sent(STRANGER_ID)
    assert reply.text == texts.UNKNOWN_COMMAND  # как на любую неизвестную команду
    assert buttons(reply) == []
    await stranger.send("/foo")
    assert session.sent(STRANGER_ID)[-1].text == reply.text  # ответы неотличимы


async def test_stranger_callback_is_refused_without_edit(
    stranger: FakeUser,
    repo: SqliteRepository,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (app_id,) = await seed(repo, [NEW])
    forged = [
        PanelCallback(a=PanelAction.HOME),
        PanelCallback(a=PanelAction.CARD, i=app_id),
        PanelCallback(a=PanelAction.SET, i=app_id, t="cancelled"),
        PanelCallback(a=PanelAction.SET, i="x\nCRITICAL поддельная строка", t="done"),
    ]
    for data in forged:
        await stranger.press(data.pack())
    assert [a.text for a in session.answers()] == [texts.STALE_BUTTON] * len(forged)
    assert session.edits() == [] and session.markup_edits() == []
    assert session.sent(ADMIN_ID) == []
    assert (await repo.get(app_id)).status is NEW  # type: ignore[union-attr]
    assert all("\n" not in r.getMessage() for r in caplog.records)
    assert "отказ в доступе" in caplog.text


# ---------- списки и пагинация ----------


@pytest.mark.parametrize("count", [0, 1, 5, 6, 11])
async def test_pagination(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession, count: int
) -> None:
    ids = await seed(repo, [NEW] * count)
    await open_list(admin_user, session, "📋 Все")
    pages = max(1, -(-count // 5))
    seen: list[str] = []
    for page in range(pages):
        text, markup = screen(session)
        app_buttons = [b for b in labels(markup) if b.startswith("🆕 261019-")]
        seen += [b.removeprefix("🆕 ") for b in app_buttons]
        if count == 0:
            assert text == texts.PANEL_LIST_EMPTY.format(title="📋 Все заявки")
        else:
            assert f"страница {page + 1} из {pages}" in text
            assert len(app_buttons) == min(5, count - page * 5)
        assert (texts.PANEL_BTN_PREV in labels(markup)) == (page > 0)
        assert (texts.PANEL_BTN_NEXT in labels(markup)) == (page + 1 < pages)
        assert texts.PANEL_BTN_HOME in labels(markup)
        if page + 1 < pages:
            await press(admin_user, session, texts.PANEL_BTN_NEXT)
    assert seen == ids  # все заявки, по порядку «новые сверху», без повторов
    if pages > 1:
        await press(admin_user, session, texts.PANEL_BTN_PREV)
        assert f"страница {pages - 1} из {pages}" in screen(session)[0]


async def test_page_out_of_range_is_clamped(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await seed(repo, [NEW] * 6)
    await admin_user.send("/admin")
    await admin_user.press(PanelCallback(a=PanelAction.LIST, p=99).pack())
    assert "страница 2 из 2" in screen(session)[0]
    await admin_user.press(PanelCallback(a=PanelAction.LIST, f="bogus", p=-3).pack())
    assert "страница 1 из 2" in screen(session)[0]  # неизвестный фильтр → «все»


async def test_list_by_status_shows_only_that_status(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    ids = await seed(repo, [NEW, IN_WORK, DONE, CANCELLED, IN_WORK])
    await open_list(admin_user, session, "🔧 В работе (2)")
    text, markup = screen(session)
    assert text.startswith("<b>🔧 В работе</b> — 2 заявки")
    assert [b for b in labels(markup) if "261019-" in b] == [f"🔧 {ids[1]}", f"🔧 {ids[4]}"]
    # В списке нет контакта и текста задачи
    assert "anna@mail.ru" not in text and "автошколы" not in text
    assert "Анна" in text and "Лендинг" in text and "19.10.2026 11:59" in text


# ---------- карточка и статусы ----------


@pytest.mark.parametrize(
    ("status", "actions"),
    [
        (NEW, ["🔧 В работу", "🚫 Отменить"]),
        (IN_WORK, ["✅ Завершить", "🚫 Отменить", "↩️ Вернуть в новые"]),
        (DONE, ["↩️ Вернуть в новые"]),
        (CANCELLED, ["↩️ Вернуть в новые"]),
    ],
)
async def test_card_buttons_by_status(
    admin_user: FakeUser,
    repo: SqliteRepository,
    session: MockSession,
    status: ApplicationStatus,
    actions: list[str],
) -> None:
    (app_id,) = await seed(repo, [status])
    await admin_user.send("/admin")
    await admin_user.press(PanelCallback(a=PanelAction.CARD, i=app_id).pack())
    text, markup = screen(session)
    assert labels(markup) == [texts.BTN_WRITE_TO_CLIENT, *actions, texts.PANEL_BTN_BACK_TO_LIST]
    assert markup.inline_keyboard[0][0].url == "https://t.me/anna_k"
    assert text.startswith(f"<b>Заявка № {app_id}</b>")
    assert "<b>Контакт:</b> anna@mail.ru" in text  # в карточке — полные данные
    assert f"<b>Статус:</b> {status.icon} {status.label}" in text
    assert ("В работе с" in text) == (status in (IN_WORK, DONE))
    assert ("Закрыта" in text) == (status in (DONE, CANCELLED))


async def test_status_flow_through_panel(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    (app_id,) = await seed(repo, [NEW])
    await open_list(admin_user, session, "🆕 Новые (1)")
    await press(admin_user, session, f"🆕 {app_id}")
    await press(admin_user, session, "🔧 В работу")
    assert session.answers()[-1].text == "Статус: В работе."
    assert (await repo.get(app_id)).status is IN_WORK  # type: ignore[union-attr]
    assert "В работе с " in screen(session)[0]

    await press(admin_user, session, "✅ Завершить")
    record = await repo.get(app_id)
    assert record is not None and record.status is DONE and record.closed_at is not None
    await press(admin_user, session, "↩️ Вернуть в новые")
    record = await repo.get(app_id)
    assert record is not None and (record.status, record.taken_at, record.closed_at) == (
        NEW,
        None,
        None,
    )
    await press(admin_user, session, texts.PANEL_BTN_BACK_TO_LIST)
    assert screen(session)[0].startswith("<b>🆕 Новые</b> — 1 заявка")


async def test_cancel_requires_confirmation(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    (app_id,) = await seed(repo, [IN_WORK])
    await admin_user.send("/admin")
    await admin_user.press(PanelCallback(a=PanelAction.CARD, i=app_id).pack())

    await press(admin_user, session, "🚫 Отменить")
    text, markup = screen(session)
    assert f"Отменить заявку № {app_id}?" in text
    assert labels(markup) == [texts.PANEL_BTN_CONFIRM_CANCEL, texts.PANEL_BTN_KEEP]
    assert (await repo.get(app_id)).status is IN_WORK  # type: ignore[union-attr]

    await press(admin_user, session, texts.PANEL_BTN_KEEP)  # «Нет» ничего не меняет
    assert (await repo.get(app_id)).status is IN_WORK  # type: ignore[union-attr]
    assert "🚫 Отменить" in labels(screen(session)[1])

    await press(admin_user, session, "🚫 Отменить")
    await press(admin_user, session, texts.PANEL_BTN_CONFIRM_CANCEL)
    record = await repo.get(app_id)
    assert record is not None and record.status is CANCELLED and record.closed_at is not None
    assert session.answers()[-1].text == "Статус: Отменена."
    assert len(await repo.list_by_status(CANCELLED, 5, 0)) == 1  # не удалена


async def test_stale_status_buttons(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    (app_id,) = await seed(repo, [DONE])
    await admin_user.send("/admin")
    # Старая кнопка «Завершить»: заявка уже выполнена
    await admin_user.press(PanelCallback(a=PanelAction.SET, i=app_id, t="done").pack())
    assert session.answers()[-1].text == "Заявка уже в статусе «Выполнена»."
    # Старая кнопка «В работу»: из done так нельзя
    await admin_user.press(PanelCallback(a=PanelAction.SET, i=app_id, t="in_work").pack())
    assert session.answers()[-1].text == "Так изменить статус нельзя: заявка сейчас «Выполнена»."
    # Старый экран подтверждения отмены для закрытой заявки → актуальная карточка
    await admin_user.press(PanelCallback(a=PanelAction.ASK_CANCEL, i=app_id).pack())
    assert "Отменить заявку" not in screen(session)[0]
    # Неизвестный целевой статус
    await admin_user.press(PanelCallback(a=PanelAction.SET, i=app_id, t="archived").pack())
    assert session.answers()[-1].text == texts.STALE_BUTTON
    assert (await repo.get(app_id)).status is DONE  # type: ignore[union-attr]


async def test_deleted_application(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession, tmp_path: Path
) -> None:
    (app_id,) = await seed(repo, [NEW])
    with sqlite3.connect(tmp_path / "bot.db", autocommit=True) as conn:
        conn.execute("DELETE FROM applications")  # удалили по запросу клиента
    conn.close()
    await admin_user.send("/admin")
    for data in (
        PanelCallback(a=PanelAction.CARD, i=app_id),
        PanelCallback(a=PanelAction.SET, i=app_id, t="in_work"),
        PanelCallback(a=PanelAction.ASK_CANCEL, i=app_id),
    ):
        await admin_user.press(data.pack())
        text, markup = screen(session)
        assert text == texts.PANEL_NOT_FOUND.format(app_id=app_id)
        assert labels(markup) == [texts.PANEL_BTN_HOME]
    await admin_user.press(PanelCallback(a=PanelAction.CARD, i="<b>x</b>").pack())
    assert screen(session)[0] == texts.PANEL_NOT_FOUND.format(app_id="&lt;b&gt;x&lt;/b&gt;")


# ---------- база недоступна ----------


class FailingReads(NullRepository):
    @property
    def available(self) -> bool:
        return True

    async def count_by_status(self) -> dict[ApplicationStatus, int]:
        raise sqlite3.OperationalError("database is locked: Анна")


async def test_panel_without_database(settings: Settings, bot: Bot, session: MockSession) -> None:
    dp = make_dp(settings, NullRepository())
    admin = FakeUser(dp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    await admin.send("/admin")
    assert screen(session)[0] == texts.PANEL_DB_UNAVAILABLE
    for action in PanelAction:
        await admin.press(PanelCallback(a=action, i="261019-0000", t="done").pack())
        assert screen(session)[0] == texts.PANEL_DB_UNAVAILABLE


async def test_panel_read_error(
    settings: Settings, bot: Bot, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    dp = make_dp(settings, FailingReads())
    admin = FakeUser(dp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    await admin.send("/admin")
    assert screen(session)[0] == texts.PANEL_DB_ERROR.format(error="OperationalError")
    assert "Анна" not in caplog.text and "Анна" not in screen(session)[0]


async def test_message_not_modified_is_quiet(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await admin_user.send("/admin")

    def hook(method: TelegramMethod) -> None:
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method, "Bad Request: message is not modified")
        return None

    session.hook = hook
    await admin_user.press(PanelCallback(a=PanelAction.HOME).pack())
    assert texts.UNEXPECTED_ERROR not in [m.text for m in session.sent(ADMIN_ID)]


async def test_privacy_restricted_link_is_dropped(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    app = make_app(applicant=Applicant(user_id=77, username=None, full_name="Без ника"))
    await repo.add(app.model_copy(update={"contact": "x@mail.ru"}))

    def hook(method: TelegramMethod) -> None:
        markup = getattr(method, "reply_markup", None)
        links = (
            [b.url or "" for row in markup.inline_keyboard for b in row]
            if isinstance(markup, InlineKeyboardMarkup)
            else []
        )
        if isinstance(method, EditMessageText) and any(u.startswith("tg://") for u in links):
            raise TelegramBadRequest(method, "Bad Request: BUTTON_USER_PRIVACY_RESTRICTED")
        return None

    session.hook = hook
    await admin_user.send("/admin")
    await admin_user.press(PanelCallback(a=PanelAction.CARD, i=app.id).pack())
    assert labels(screen(session)[1]) == ["🔧 В работу", "🚫 Отменить", "‹ К списку"]


async def test_inaccessible_message_gets_new_screen(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await seed(repo, [NEW])
    callback = CallbackQuery(
        id="1",
        from_user=admin_user.user,
        chat_instance="ci",
        # Telegram не отдал сообщение с кнопкой (удалено или недоступно боту).
        message=InaccessibleMessage(chat=admin_user.chat, message_id=5),
        data=PanelCallback(a=PanelAction.HOME).pack(),
    )
    await admin_user.feed(Update(update_id=10**6, callback_query=callback))
    assert session.edits() == []
    (new_screen,) = session.sent(ADMIN_ID)
    assert new_screen.text == texts.PANEL_HOME


@pytest.mark.parametrize("reason", ["message can't be edited", "message to edit not found"])
async def test_uneditable_message_gets_new_screen(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession, reason: str
) -> None:
    (app_id,) = await seed(repo, [NEW])

    def hook(method: TelegramMethod) -> None:
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method, f"Bad Request: {reason}")
        return None

    session.hook = hook
    await admin_user.press(PanelCallback(a=PanelAction.SET, i=app_id, t="in_work").pack())
    assert (await repo.get(app_id)).status is IN_WORK  # type: ignore[union-attr]
    sent = [m.text for m in session.sent(ADMIN_ID)]
    assert texts.UNEXPECTED_ERROR not in sent
    assert len(sent) == 1 and sent[0].startswith(f"<b>Заявка № {app_id}</b>")


async def test_admin_command_keeps_own_application_step(
    admin_user: FakeUser, session: MockSession
) -> None:
    """/admin посреди собственной заявки администратора не сбрасывает шаг."""
    await admin_user.send("/start")
    await admin_user.press(APPLY)
    assert session.last_sent(ADMIN_ID).text == texts.ASK_NAME
    await admin_user.send("/admin")
    assert session.last_sent(ADMIN_ID).text == texts.PANEL_HOME
    await admin_user.send("Эдуард")  # имя, а не «неизвестная команда» и не сброс
    assert session.last_sent(ADMIN_ID).text == texts.ASK_CONTACT


# ---------- экранирование и длина ----------


async def test_html_is_escaped_and_fits_telegram_limit(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    astral = "😀"
    evil_name = ("<b>&" + astral) * 20  # 80 символов
    for n in range(5):
        app = make_app(
            f"261019-{n:04X}",
            name=evil_name,
            contact="a" * 64 + "@" + astral * 52 + ".ru",
            task=(astral + "&<") * 333 + astral,
            project_type=ProjectType.SHOP,
            applicant=Applicant(
                user_id=9_999_999_999, username="u" * 32, full_name=astral * 64 + "<i>" * 21
            ),
        )
        await repo.add(app)
    await repo.set_status("261019-0000", IN_WORK, T0)
    await repo.set_status("261019-0000", DONE, T0)

    await open_list(admin_user, session, "📋 Все")
    page, _ = screen(session)
    assert "&lt;b&gt;&amp;😀" in page and "<b>&" not in page
    plain, _ = as_telegram_sees(page)  # разметка сбалансирована
    assert len(plain.encode("utf-16-le")) // 2 <= 4096

    await admin_user.press(PanelCallback(a=PanelAction.CARD, i="261019-0000").pack())
    card, _ = screen(session)
    assert "&lt;i&gt;" in card and "<i>" not in card
    plain, _ = as_telegram_sees(card)
    assert len(plain.encode("utf-16-le")) // 2 <= 4096
    await admin_user.press(PanelCallback(a=PanelAction.ASK_CANCEL, i="261019-0001").pack())
    plain, _ = as_telegram_sees(screen(session)[0])
    assert len(plain.encode("utf-16-le")) // 2 <= 4096


def test_callback_data_fits_64_bytes() -> None:
    longest = PanelCallback(
        a=PanelAction.SET, f="cancelled", p=99_999, i="261019-ABCD", t="cancelled"
    )
    assert len(longest.pack().encode()) <= 64


# ---------- «В работу» под уведомлением ----------


async def submit_application(dp: Dispatcher, bot: Bot, session: MockSession) -> SendMessage:
    client = FakeUser(
        dp,
        bot,
        session,
        User(id=CLIENT_ID, is_bot=False, first_name="Анна", last_name="К", username="anna_k"),
    )
    await fill_until_summary(client, session)
    await client.press(SUBMIT)
    return [m for m in session.sent(ADMIN_ID) if "Новая заявка №" in m.text][-1]


def take_data(message: SendMessage) -> str:
    return data_of(message.reply_markup, texts.BTN_TAKE)  # type: ignore[arg-type]


async def test_take_writes_status_with_same_time(
    settings: Settings,
    panel_dp: Dispatcher,
    admin_user: FakeUser,
    repo: SqliteRepository,
    bot: Bot,
    session: MockSession,
) -> None:
    notification = await submit_application(panel_dp, bot, session)
    app_id = notification.text.split("№ ")[1].split("<")[0]
    await admin_user.press(take_data(notification), on=notification)

    record = await repo.get(app_id)
    assert record is not None and record.status is IN_WORK and record.taken_at is not None
    mark = format_local_time(record.taken_at, settings.tz)
    assert session.edits()[-1].text.endswith(f"✅ <b>В работе</b> с {mark}")
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_ALERT
    assert not [m for m in session.sent(ADMIN_ID) if m.text.startswith("⚠️")]

    # Нажатие на старом экране (кнопка ещё видна): время в базе и в отметке не сдвигается.
    await admin_user.press(take_data(notification), on=notification)
    assert session.edits()[-1].text.endswith(f"✅ <b>В работе</b> с {mark}")
    assert (await repo.get(app_id)).taken_at == record.taken_at  # type: ignore[union-attr]


async def test_take_after_panel_uses_stored_time(
    settings: Settings,
    panel_dp: Dispatcher,
    admin_user: FakeUser,
    repo: SqliteRepository,
    bot: Bot,
    session: MockSession,
) -> None:
    notification = await submit_application(panel_dp, bot, session)
    app_id = notification.text.split("№ ")[1].split("<")[0]
    earlier = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)
    await repo.set_status(app_id, IN_WORK, earlier)  # взяли в работу из панели
    await admin_user.press(take_data(notification), on=notification)
    assert session.edits()[-1].text.endswith(
        f"✅ <b>В работе</b> с {format_local_time(earlier, settings.tz)}"
    )
    assert (await repo.get(app_id)).taken_at == earlier  # type: ignore[union-attr]


async def test_take_on_closed_application_does_not_lie(
    panel_dp: Dispatcher,
    admin_user: FakeUser,
    repo: SqliteRepository,
    bot: Bot,
    session: MockSession,
) -> None:
    notification = await submit_application(panel_dp, bot, session)
    app_id = notification.text.split("№ ")[1].split("<")[0]
    await repo.set_status(app_id, CANCELLED, T0)
    edits = len(session.edits())
    await admin_user.press(take_data(notification), on=notification)
    assert len(session.edits()) == edits  # «В работе» в сообщении не появилось
    answer = session.answers()[-1]
    assert answer.show_alert and "«Отменена»" in (answer.text or "")
    assert (await repo.get(app_id)).status is CANCELLED  # type: ignore[union-attr]


async def test_take_when_application_not_in_database(
    panel_dp: Dispatcher,
    admin_user: FakeUser,
    repo: SqliteRepository,
    bot: Bot,
    session: MockSession,
    tmp_path: Path,
) -> None:
    notification = await submit_application(panel_dp, bot, session)
    with sqlite3.connect(tmp_path / "bot.db", autocommit=True) as conn:
        conn.execute("DELETE FROM applications")
    conn.close()
    await admin_user.press(take_data(notification), on=notification)
    assert "✅ <b>В работе</b> с " in session.edits()[-1].text  # сообщение отмечено
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_NOT_IN_DB


class BrokenStatus(NullRepository):
    """База «есть», заявки пишутся, но смена статуса падает."""

    @property
    def available(self) -> bool:
        return True

    async def set_status(self, app_id, status, now):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError(f"database is locked {app_id}")


@pytest.mark.parametrize(
    ("repository", "error"),
    [(BrokenStatus(), "OperationalError"), (NullRepository(), texts.DB_UNAVAILABLE_REASON)],
)
async def test_take_with_database_failure_marks_and_alerts(
    settings: Settings,
    bot: Bot,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
    repository: ApplicationRepository,
    error: str,
) -> None:
    dp = make_dp(settings, repository)
    dp["storage_alerts"] = StorageAlerts(ADMIN_ID)
    admin = FakeUser(dp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    notification = await submit_application(dp, bot, session)
    app_id = notification.text.split("№ ")[1].split("<")[0]
    session.clear()
    await admin.press(take_data(notification), on=notification)

    assert "✅ <b>В работе</b> с " in session.edits()[-1].text  # отметка стоит
    (alert,) = session.sent(ADMIN_ID)
    assert alert.text == texts.ADMIN_DB_STATUS_FAILED.format(
        app_id=app_id, status="В работе", error=error
    )
    for personal in ("Анна", "anna@mail.ru", "anna_k"):
        assert personal not in caplog.text and personal not in alert.text


# ---------- меню команд ----------


async def test_admin_menu_scope(settings: Settings, bot: Bot, session: MockSession) -> None:
    dp = make_dp(settings, NullRepository())
    await dp.emit_startup(bot=bot, **dp.workflow_data)
    menus = [m for m in session.requests if isinstance(m, SetMyCommands)]
    assert len(menus) == 2
    common, admin_menu = menus
    assert isinstance(common.scope, BotCommandScopeAllPrivateChats)
    assert "admin" not in [c.command for c in common.commands]
    assert isinstance(admin_menu.scope, BotCommandScopeChat)
    assert admin_menu.scope.chat_id == ADMIN_ID
    assert [c.command for c in admin_menu.commands] == ["start", "help", "cancel", "admin"]
    assert admin_menu.commands[-1].description == texts.CMD_ADMIN


async def test_admin_menu_failure_does_not_break_startup(
    settings: Settings, bot: Bot, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    def hook(method: TelegramMethod) -> None:
        if isinstance(method, SetMyCommands) and isinstance(method.scope, BotCommandScopeChat):
            raise TelegramBadRequest(method, "Bad Request: chat not found")
        return None

    session.hook = hook
    caplog.set_level(logging.WARNING)
    notify = settings.model_copy(update={"notify_admin_on_start": True})
    dp = make_dp(notify, NullRepository())
    await dp.emit_startup(bot=bot, **dp.workflow_data)
    assert "Не удалось установить меню администратора: TelegramBadRequest" in caplog.text
    assert [m.text for m in session.sent(ADMIN_ID)] == [texts.ADMIN_STARTED]  # старт продолжился
