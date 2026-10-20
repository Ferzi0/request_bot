"""Сообщение клиенту «заявка принята в работу» (этап 2в).

Все базы — во временных папках pytest; data/ проекта тесты не трогают.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    ClientDecodeError,
    RestartingTelegram,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import User

from bot import texts
from bot.callbacks import PanelAction, PanelCallback
from bot.config import Settings
from bot.models import ApplicationStatus
from bot.services import sqlite_repository
from bot.services.client_notifier import ClientNotice, ClientNotifier, format_client_taken
from bot.services.sqlite_repository import SqliteRepository
from bot.services.storage import NullRepository
from tests.conftest import ADMIN_ID, CLIENT_ID, FakeUser, MockSession, as_telegram_sees
from tests.test_panel import make_dp, submit_application, take_data
from tests.test_storage import connect, make_app, rows

IN_WORK, NEW, DONE, CANCELLED = (
    ApplicationStatus.IN_WORK,
    ApplicationStatus.NEW,
    ApplicationStatus.DONE,
    ApplicationStatus.CANCELLED,
)
T0 = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
PERSONAL = ("Анна", "anna@mail.ru", "anna_k", "автошколы")


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[SqliteRepository]:
    async with SqliteRepository(tmp_path / "bot.db") as repository:
        yield repository


@pytest.fixture
def ndp(settings: Settings, repo: SqliteRepository) -> Dispatcher:
    return make_dp(settings, repo)


@pytest.fixture
def admin_user(ndp: Dispatcher, bot: Bot, session: MockSession) -> FakeUser:
    return FakeUser(ndp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Эдуард"))


def client_notices(session: MockSession) -> list[SendMessage]:
    return [m for m in session.sent(CLIENT_ID) if "принята в работу" in m.text]


def admin_failures(session: MockSession) -> list[str]:
    return [m.text for m in session.sent(ADMIN_ID) if m.text.startswith("Клиенту по заявке")]


def number_of(notification: SendMessage) -> str:
    return notification.text.split("№ ")[1].split("<")[0]


async def take_from_panel(admin: FakeUser, app_id: str, target: ApplicationStatus) -> None:
    await admin.press(PanelCallback(a=PanelAction.SET, i=app_id, t=target.value).pack())


def fail_client_with(exc_factory):  # type: ignore[no-untyped-def]
    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        to_client = isinstance(method, SendMessage) and int(method.chat_id) == CLIENT_ID
        if to_client and "принята в работу" in method.text:
            raise exc_factory(method)
        return None

    return hook


# ---------- текст ----------


def test_client_text_is_the_approved_one() -> None:
    text = format_client_taken("261020-676A")
    assert text == (
        "🔧 <b>Заявка № <code>261020-676A</code> принята в работу</b>\n"
        "Эдуард начал её рассматривать и свяжется с вами по указанному контакту."
    )
    _, entities = as_telegram_sees(text)
    assert {e.type for e in entities} == {"bold", "code"}
    assert "<b>x</b>" not in format_client_taken("<b>x</b>")  # номер экранируется


# ---------- когда уходит ----------


async def test_take_button_notifies_client_once(
    ndp: Dispatcher,
    admin_user: FakeUser,
    repo: SqliteRepository,
    bot: Bot,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    notification = await submit_application(ndp, bot, session)
    app_id = number_of(notification)
    await admin_user.press(take_data(notification), on=notification)

    (notice,) = client_notices(session)
    assert notice.text == format_client_taken(app_id)
    record = await repo.get(app_id)
    assert record is not None and record.client_notified_at is not None
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_ALERT
    assert admin_failures(session) == []
    # Порядок: отметка у администратора и ответ на нажатие — до сообщения клиенту
    order = [type(m).__name__ for m in session.requests[-3:]]
    assert order == ["EditMessageText", "AnswerCallbackQuery", "SendMessage"]

    # Старый экран (кнопка ещё видна) — повторное «В работу» клиенту не пишет
    await admin_user.press(take_data(notification), on=notification)
    # «Вернуть в новые» → снова «В работу» — тоже без второго сообщения
    await take_from_panel(admin_user, app_id, NEW)
    await take_from_panel(admin_user, app_id, IN_WORK)
    assert len(client_notices(session)) == 1
    for personal in PERSONAL:
        assert personal not in caplog.text


async def test_panel_take_notifies_client(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await repo.add(make_app("261020-0001"))
    await take_from_panel(admin_user, "261020-0001", IN_WORK)
    (notice,) = client_notices(session)
    assert int(notice.chat_id) == CLIENT_ID  # applicant_user_id из базы
    assert session.answers()[-1].text == "Статус: В работе."


@pytest.mark.parametrize(
    ("path", "last"),
    [
        ([], CANCELLED),  # new → cancelled
        ([IN_WORK], DONE),  # in_work → done
        ([IN_WORK], CANCELLED),
        ([IN_WORK], NEW),  # «Вернуть в новые»
    ],
)
async def test_other_transitions_do_not_notify(
    admin_user: FakeUser,
    repo: SqliteRepository,
    session: MockSession,
    path: list[ApplicationStatus],
    last: ApplicationStatus,
) -> None:
    await repo.add(make_app("261020-0001"))
    for step in path:
        await repo.set_status("261020-0001", step, T0)  # мимо бота — без уведомления
    await take_from_panel(admin_user, "261020-0001", last)
    assert (await repo.get("261020-0001")).status is last  # type: ignore[union-attr]
    assert client_notices(session) == []


async def test_already_in_work_does_not_notify(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await repo.add(make_app("261020-0001"))
    await repo.set_status("261020-0001", IN_WORK, T0)  # взяли в работу без бота
    await take_from_panel(admin_user, "261020-0001", IN_WORK)  # «уже в работе»
    assert client_notices(session) == []


async def test_concurrent_takes_send_one_message(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    db = tmp_path / "bot.db"
    async with SqliteRepository(db) as first, SqliteRepository(db) as second:
        for i in range(10):
            app_id = f"261020-{i:04X}"
            await first.add(make_app(app_id))
            await first.set_status(app_id, IN_WORK, T0)
            notifier = ClientNotifier(ADMIN_ID)
            results = await asyncio.gather(
                notifier.application_taken(bot, first, app_id),
                notifier.application_taken(bot, second, app_id),
            )
            assert sorted(results) == sorted([ClientNotice.SENT, ClientNotice.SKIPPED]), i
    assert len(client_notices(session)) == 10


# ---------- сбои отправки ----------


async def test_blocked_client(
    ndp: Dispatcher,
    admin_user: FakeUser,
    repo: SqliteRepository,
    bot: Bot,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    notification = await submit_application(ndp, bot, session)
    app_id = number_of(notification)
    # Текст исключения с данными клиента: в лог и администратору — только класс.
    session.hook = fail_client_with(
        lambda m: TelegramForbiddenError(m, "Forbidden: Анна anna@mail.ru anna_k")
    )
    await admin_user.press(take_data(notification), on=notification)

    assert "✅ <b>В работе</b> с " in session.edits()[-1].text  # отметка стоит
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_ALERT  # нажатие отвечено
    record = await repo.get(app_id)
    assert record is not None and record.status is IN_WORK
    assert record.client_notified_at is not None  # повторять бессмысленно — метка остаётся
    assert admin_failures(session) == [
        texts.ADMIN_CLIENT_NOTICE_FAILED.format(app_id=app_id, error="TelegramForbiddenError")
    ]
    assert texts.UNEXPECTED_ERROR not in [m.text for m in session.sent(ADMIN_ID)]
    for personal in PERSONAL:
        assert personal not in caplog.text and personal not in admin_failures(session)[0]

    session.hook = None  # клиент разблокировал — но повтор не идёт: без дублей
    await take_from_panel(admin_user, app_id, NEW)
    await take_from_panel(admin_user, app_id, IN_WORK)
    assert len([m for m in session.sent(CLIENT_ID) if "принята в работу" in m.text]) == 1


async def test_permanent_bad_request_keeps_mark(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await repo.add(make_app("261020-0001"))
    session.hook = fail_client_with(lambda m: TelegramBadRequest(m, "Bad Request: chat not found"))
    await take_from_panel(admin_user, "261020-0001", IN_WORK)
    assert (await repo.get("261020-0001")).client_notified_at is not None  # type: ignore[union-attr]
    assert "TelegramBadRequest" in admin_failures(session)[0]


async def test_network_error_releases_mark_for_retry(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    await repo.add(make_app("261020-0001"))
    session.hook = fail_client_with(lambda m: TelegramNetworkError(m, "timeout"))
    await take_from_panel(admin_user, "261020-0001", IN_WORK)
    record = await repo.get("261020-0001")
    assert record is not None and record.status is IN_WORK
    assert record.client_notified_at is None  # временный сбой — метка снята
    assert admin_failures(session) == [
        texts.ADMIN_CLIENT_NOTICE_FAILED.format(app_id="261020-0001", error="TelegramNetworkError")
    ]

    session.hook = None  # сеть вернулась: следующий переход в работу отправит
    await take_from_panel(admin_user, "261020-0001", NEW)
    await take_from_panel(admin_user, "261020-0001", IN_WORK)
    delivered = [m for m in session.sent(CLIENT_ID) if "принята в работу" in m.text]
    assert len(delivered) == 2  # 1 упавшая попытка + 1 дошедшая (MockSession пишет обе)
    assert (await repo.get("261020-0001")).client_notified_at is not None  # type: ignore[union-attr]


async def test_admin_notice_failure_is_swallowed(
    admin_user: FakeUser,
    repo: SqliteRepository,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, SendMessage):
            raise TelegramNetworkError(method, "down")  # не уходит ни клиенту, ни админу
        return None

    await repo.add(make_app("261020-0001"))
    session.hook = hook
    await take_from_panel(admin_user, "261020-0001", IN_WORK)
    assert (await repo.get("261020-0001")).status is IN_WORK  # type: ignore[union-attr]
    assert "не удалось сообщить администратору о сбое: TelegramNetworkError" in caplog.text


# ---------- база ----------


class ClaimFails(NullRepository):
    @property
    def available(self) -> bool:
        return True

    async def claim_client_notice(self, app_id: str, now: datetime) -> int | None:
        raise sqlite3.OperationalError("database is locked: Анна")


async def test_claim_error_tells_admin_and_skips_client(bot: Bot, session: MockSession) -> None:
    result = await ClientNotifier(ADMIN_ID).application_taken(bot, ClaimFails(), "261020-0001")
    assert result is ClientNotice.FAILED
    assert session.sent(CLIENT_ID) == []
    assert admin_failures(session) == [
        texts.ADMIN_CLIENT_NOTICE_FAILED.format(app_id="261020-0001", error="OperationalError")
    ]


async def test_without_database_client_is_not_notified(
    settings: Settings, bot: Bot, session: MockSession
) -> None:
    dp = make_dp(settings, NullRepository())
    admin = FakeUser(dp, bot, session, User(id=ADMIN_ID, is_bot=False, first_name="Э"))
    notification = await submit_application(dp, bot, session)
    await admin.press(take_data(notification), on=notification)
    assert "✅ <b>В работе</b> с " in session.edits()[-1].text
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_NO_CLIENT_NOTICE
    assert client_notices(session) == []


async def test_not_in_database_client_is_not_notified(
    ndp: Dispatcher,
    admin_user: FakeUser,
    bot: Bot,
    session: MockSession,
    tmp_path: Path,
) -> None:
    notification = await submit_application(ndp, bot, session)
    with connect(tmp_path / "bot.db") as conn:
        conn.execute("DELETE FROM applications")
    await admin_user.press(take_data(notification), on=notification)
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_NOT_IN_DB
    assert "клиенту не сообщено" in texts.ADMIN_TAKEN_NOT_IN_DB
    assert client_notices(session) == []


# ---------- репозиторий: метка ----------


async def test_claim_is_atomic_and_release_only_own(repo: SqliteRepository) -> None:
    await repo.add(make_app("261020-0001"))
    assert await repo.claim_client_notice("261020-0001", T0) is None  # ещё новая
    await repo.set_status("261020-0001", IN_WORK, T0)
    assert await repo.claim_client_notice("261020-0001", T0) == CLIENT_ID
    assert await repo.claim_client_notice("261020-0001", T0) is None  # второй раз — нет
    later = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)
    await repo.release_client_notice("261020-0001", later)  # чужое время — не снимает
    assert (await repo.get("261020-0001")).client_notified_at == T0  # type: ignore[union-attr]
    await repo.release_client_notice("261020-0001", T0)
    assert (await repo.get("261020-0001")).client_notified_at is None  # type: ignore[union-attr]
    assert await repo.claim_client_notice("000000-0000", T0) is None
    assert await repo.claim_client_notice("' OR 1=1 --", T0) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.claim_client_notice("261020-0001", datetime(2026, 1, 1))


async def test_null_repository_claim() -> None:
    null = NullRepository()
    assert await null.claim_client_notice("x", T0) is None
    await null.release_client_notice("x", T0)


async def test_migrates_v3_file_with_data(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    with connect(db) as conn:  # база этапа 2б: схема v3, заявка в работе
        conn.executescript("".join(sqlite_repository.MIGRATIONS[:3]) + "\nPRAGMA user_version = 3;")
        conn.execute(
            "INSERT INTO applications (id, created_at, source, name, contact, project_type,"
            " task, consent, applicant_user_id, applicant_full_name, status, taken_at)"
            " VALUES ('261019-AAAA', '2026-10-19T09:00:00Z', 'bot', 'Олег', 'o@mail.ru',"
            " 'shop', 'Магазин запчастей', 1, 42, 'Олег', 'in_work', '2026-10-19T10:00:00Z')"
        )
    async with SqliteRepository(db) as repository:
        assert (await repository.status()).schema_version == 4
        old = await repository.get("261019-AAAA")
        assert old is not None and old.status is IN_WORK and old.client_notified_at is None
        # Старой заявке «в работе» клиенту не писали — но и сейчас не напишем:
        # уведомление уходит только при переходе new → in_work.
        assert old.taken_at == datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    (row,) = rows(db)
    assert row["client_notified_at"] is None and row["task"] == "Магазин запчастей"
    with connect(db) as conn:
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'applications'").fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE applications SET client_notified_at = '20.10.2026'")
    assert "-- Когда клиенту отправлено «заявка принята в работу»" in ddl[0]


# ---------- кто вызывает уведомитель ----------


class SpyNotifier(ClientNotifier):
    def __init__(self) -> None:
        super().__init__(ADMIN_ID)
        self.calls: list[str] = []

    async def application_taken(self, bot, repository, app_id):  # type: ignore[no-untyped-def]
        self.calls.append(app_id)
        return await super().application_taken(bot, repository, app_id)


async def test_notifier_called_only_on_new_to_in_work(
    ndp: Dispatcher, admin_user: FakeUser, repo: SqliteRepository
) -> None:
    """Одно место вызова — и только на переход new → in_work (не на любой «changed»)."""
    spy = SpyNotifier()
    ndp["client_notifier"] = spy
    await repo.add(make_app("261020-0001"))
    for target in (IN_WORK, DONE, NEW, CANCELLED, NEW, IN_WORK, NEW):
        await take_from_panel(admin_user, "261020-0001", target)
    assert spy.calls == ["261020-0001", "261020-0001"]  # два перехода new → in_work


async def test_take_button_on_already_taken_does_not_notify(
    ndp: Dispatcher,
    admin_user: FakeUser,
    repo: SqliteRepository,
    bot: Bot,
    session: MockSession,
) -> None:
    """Заявку взяли в работу мимо бота (метки нет) — «В работу» под уведомлением не пишет."""
    spy = SpyNotifier()
    ndp["client_notifier"] = spy
    notification = await submit_application(ndp, bot, session)
    await repo.set_status(number_of(notification), IN_WORK, T0)
    await admin_user.press(take_data(notification), on=notification)
    assert spy.calls == [] and client_notices(session) == []


# ---------- антибаг 2в: классы ошибок, сбой снятия метки, порядок в панели ----------


def _retry_after(m: TelegramMethod) -> Exception:  # type: ignore[type-arg]
    return TelegramRetryAfter(m, "Too Many Requests", retry_after=5)


@pytest.mark.parametrize(
    ("make_error", "released"),
    [
        # Временные — метку снимаем, следующий переход в работу отправит снова.
        (lambda m: TelegramNetworkError(m, "timeout"), True),
        (lambda m: TelegramServerError(m, "Bad Gateway"), True),
        (lambda m: RestartingTelegram(m, "restarting"), True),
        (_retry_after, True),
        # Постоянные и неизвестные — метка остаётся (без дублей).
        (lambda m: TelegramForbiddenError(m, "bot was blocked by the user"), False),
        (lambda m: TelegramBadRequest(m, "chat not found"), False),
        (lambda m: TelegramNotFound(m, "Not Found"), False),
        (lambda m: TelegramUnauthorizedError(m, "Unauthorized"), False),
        (lambda m: ClientDecodeError("bad json", ValueError("x"), "{"), False),
        (lambda m: RuntimeError("неизвестно, дошло ли"), False),
    ],
    ids=lambda v: "released" if v is True else "kept" if v is False else "",
)
async def test_mark_fate_by_error_class(
    repo: SqliteRepository, bot: Bot, session: MockSession, make_error, released: bool
) -> None:  # type: ignore[no-untyped-def]
    await repo.add(make_app("261020-0001"))
    await repo.set_status("261020-0001", IN_WORK, T0)
    session.hook = fail_client_with(make_error)
    notifier = ClientNotifier(ADMIN_ID, clock=lambda: T0)
    assert await notifier.application_taken(bot, repo, "261020-0001") is ClientNotice.FAILED
    mark = (await repo.get("261020-0001")).client_notified_at  # type: ignore[union-attr]
    assert (mark is None) is released
    (failure,) = admin_failures(session)
    assert type(make_error(None)).__name__ in failure


class ReleaseFails(NullRepository):
    """Метка ставится, а снять её не получается (база отвалилась между запросами)."""

    @property
    def available(self) -> bool:
        return True

    async def claim_client_notice(self, app_id: str, now: datetime) -> int | None:
        return CLIENT_ID

    async def release_client_notice(self, app_id: str, claimed_at: datetime) -> None:
        raise sqlite3.OperationalError("disk I/O error: Анна")


async def test_release_error_is_swallowed_and_admin_told(
    bot: Bot, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    session.hook = fail_client_with(lambda m: TelegramNetworkError(m, "timeout"))
    result = await ClientNotifier(ADMIN_ID).application_taken(bot, ReleaseFails(), "261020-0001")
    assert result is ClientNotice.FAILED
    assert admin_failures(session) == [
        texts.ADMIN_CLIENT_NOTICE_FAILED.format(app_id="261020-0001", error="TelegramNetworkError")
    ]
    assert "метку уведомления не снять: OperationalError" in caplog.text
    assert "Анна" not in caplog.text


async def test_panel_order_screen_before_client(
    admin_user: FakeUser, repo: SqliteRepository, session: MockSession
) -> None:
    """Панель: ответ на нажатие и экран — до сообщения клиенту (сбой клиента их не задевает)."""
    await repo.add(make_app("261020-0001"))
    await take_from_panel(admin_user, "261020-0001", IN_WORK)
    order = [type(m).__name__ for m in session.requests[-3:]]
    assert order == ["AnswerCallbackQuery", "EditMessageText", "SendMessage"]
    assert int(session.requests[-1].chat_id) == CLIENT_ID  # type: ignore[attr-defined]
