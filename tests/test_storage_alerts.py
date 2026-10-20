"""Состояние базы для администратора, отметка о доставке, миграция v1 → v2.

Все базы — во временных папках pytest; data/ проекта тесты не трогают.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import User

from bot import texts
from bot.app import create_dispatcher
from bot.config import Settings, load_settings
from bot.models import Application
from bot.services import sqlite_repository
from bot.services.sqlite_repository import SqliteRepository
from bot.services.storage import DatabaseStatus
from bot.services.storage_alerts import StorageAlerts
from tests.conftest import ADMIN_ID, FAKE_TOKEN, FakeUser, MockSession
from tests.test_delivery import fail_admin_with
from tests.test_flow import SUBMIT, TASK, fill_until_summary
from tests.test_storage import connect, make_app, mode, rows

PERSONAL = ("Анна", "anna@mail.ru", TASK, "anna_k")


def make_settings(tmp_path: Path, *, notify: bool = False) -> Settings:
    return load_settings(
        _env_file=None,
        bot_token=FAKE_TOKEN,
        admin_id=ADMIN_ID,
        send_retry_delays=(0.01,),
        notify_admin_on_start=notify,
        database_path=tmp_path / "bot.db",
    )


class Clock:
    """Управляемые «часы» для ограничения частоты уведомлений."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class BrokenRepository:
    """add и/или mark_delivered бросают заданное исключение."""

    def __init__(
        self,
        add_error: Callable[[], Exception] | None = None,
        mark_error: Callable[[], Exception] | None = None,
    ) -> None:
        self.add_error, self.mark_error = add_error, mark_error
        self.marked: list[str] = []

    async def add(self, application: Application) -> None:
        if self.add_error:
            raise self.add_error()

    async def mark_delivered(self, app_id: str, delivered_at: datetime) -> None:
        if self.mark_error:
            raise self.mark_error()
        self.marked.append(app_id)


def dispatcher(settings: Settings, repository: object, clock: Clock | None = None) -> Dispatcher:
    dp = create_dispatcher(settings, repository=repository)  # type: ignore[arg-type]
    if clock is not None:
        dp["storage_alerts"] = StorageAlerts(ADMIN_ID, clock=clock)
    return dp


_user_ids = iter(range(7000, 8000))


def new_client(dp: Dispatcher, bot: Bot, session: MockSession) -> FakeUser:
    """Каждый раз новый человек — чтобы не упереться в антифлуд (3 заявки в час)."""
    uid = next(_user_ids)
    user = User(id=uid, is_bot=False, first_name="Анна", last_name="К", username="anna_k")
    return FakeUser(dp, bot, session, user)


async def submit(client: FakeUser, session: MockSession) -> None:
    await fill_until_summary(client, session)
    await client.press(SUBMIT)


def alerts(session: MockSession) -> list[str]:
    return [m.text for m in session.sent(ADMIN_ID) if m.text.startswith("⚠️")]


# ---------- стартовое сообщение ----------


async def start(dp: Dispatcher, bot: Bot) -> None:
    # Так же, как это делает dp.start_polling: workflow_data идут в on_startup по имени.
    await dp.emit_startup(bot=bot, **dp.workflow_data)


async def test_startup_reports_working_database(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    import bot.__main__ as entry

    settings = make_settings(tmp_path, notify=True)
    repository, status = await entry.open_repository(settings)
    assert repository is not None
    await repository.add(make_app("261019-0001"))
    await repository.add(make_app("261019-0002"))
    status = await repository.status()
    await repository.close()

    await start(create_dispatcher(settings, database=status), bot)
    (message,) = session.sent(ADMIN_ID)
    assert message.text == (
        texts.ADMIN_STARTED + "\n\nБаза данных: работает (схема v3, заявок: 2)."
    )


async def test_startup_reports_unavailable_database(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    import bot.__main__ as entry

    blocker = tmp_path / "file"
    blocker.write_text("не папка")
    settings = make_settings(tmp_path, notify=True).model_copy(
        update={"database_path": blocker / "bot.db"}
    )
    repository, status = await entry.open_repository(settings)
    assert repository is None

    await start(create_dispatcher(settings, database=status), bot)
    (message,) = session.sent(ADMIN_ID)
    assert message.text == texts.ADMIN_STARTED + "\n\n" + texts.ADMIN_DB_UNAVAILABLE.format(
        error="FileExistsError"
    )
    assert str(tmp_path) not in message.text and "/Users" not in message.text  # без путей


async def test_startup_without_notify_reports_only_database_problem(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    settings = make_settings(tmp_path, notify=False)
    await start(create_dispatcher(settings, database=DatabaseStatus(schema_version=2)), bot)
    assert session.sent(ADMIN_ID) == []  # база в порядке — молчим, как просили

    await start(create_dispatcher(settings, database=DatabaseStatus(error="SchemaError")), bot)
    (message,) = session.sent(ADMIN_ID)
    assert message.text == texts.ADMIN_DB_UNAVAILABLE.format(error="SchemaError")


async def test_startup_without_database_info_sends_plain_greeting(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    await start(create_dispatcher(make_settings(tmp_path, notify=True)), bot)
    assert [m.text for m in session.sent(ADMIN_ID)] == [texts.ADMIN_STARTED]


# ---------- уведомления о сбоях записи ----------


async def test_save_failures_are_throttled_per_error_class(
    tmp_path: Path, bot: Bot, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    clock = Clock()
    repository = BrokenRepository(add_error=lambda: sqlite3.OperationalError("locked"))
    dp = dispatcher(make_settings(tmp_path), repository, clock)

    await submit(new_client(dp, bot, session), session)
    await submit(new_client(dp, bot, session), session)
    assert len(alerts(session)) == 1  # 2 сбоя подряд → одно уведомление
    assert len(session.sent(ADMIN_ID)) == 3  # обе заявки дошли
    # В логе — каждая ошибка, а не только первая
    assert caplog.text.count("НЕ сохранена в базу: OperationalError") == 2

    repository.add_error = lambda: sqlite3.DatabaseError("malformed")
    await submit(new_client(dp, bot, session), session)
    assert len(alerts(session)) == 2  # другой класс ошибки → новое уведомление
    assert "DatabaseError" in alerts(session)[-1]

    clock.now += 3600  # прошёл час
    repository.add_error = lambda: sqlite3.OperationalError("locked")
    await submit(new_client(dp, bot, session), session)
    assert len(alerts(session)) == 3
    for personal in PERSONAL:
        assert personal not in caplog.text
        assert all(personal not in text for text in alerts(session))


async def test_alert_goes_after_application_and_client_is_answered(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    repository = BrokenRepository(add_error=lambda: sqlite3.OperationalError("locked"))
    client = new_client(dispatcher(make_settings(tmp_path), repository), bot, session)
    await submit(client, session)
    application, alert = session.sent(ADMIN_ID)
    number = session.last_sent(client.chat.id).text.rsplit(" ", 1)[-1]
    assert f"№ {number}" in application.text
    assert alert.text == texts.ADMIN_DB_SAVE_FAILED.format(app_id=number, error="OperationalError")
    assert session.last_sent(client.chat.id).text.startswith("Заявка принята.")


async def test_failed_alert_does_not_break_application(
    tmp_path: Path, bot: Bot, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    def hook(method: TelegramMethod) -> None:
        if isinstance(method, SendMessage) and method.text.startswith("⚠️"):
            raise TelegramNetworkError(method, "timeout")
        return None

    clock = Clock()
    repository = BrokenRepository(add_error=lambda: sqlite3.OperationalError("locked"))
    dp = dispatcher(make_settings(tmp_path), repository, clock)
    session.hook = hook
    client = new_client(dp, bot, session)
    await submit(client, session)

    assert session.last_sent(client.chat.id).text.startswith("Заявка принята.")
    assert sum("Новая заявка №" in m.text for m in session.sent(ADMIN_ID)) == 1
    assert "Не удалось уведомить администратора о сбое базы: TelegramNetworkError" in caplog.text
    for personal in PERSONAL:
        assert personal not in caplog.text

    # Неотправленное уведомление не «съедает» час: следующий сбой пробует снова.
    session.hook = None
    await submit(new_client(dp, bot, session), session)
    # MockSession записывает и неудачную попытку: 1 упавшая + 1 дошедшая
    assert len(alerts(session)) == 2


async def test_mark_failure_is_reported_with_its_own_text(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    repository = BrokenRepository(mark_error=lambda: sqlite3.OperationalError("locked"))
    client = new_client(dispatcher(make_settings(tmp_path), repository), bot, session)
    await submit(client, session)
    number = session.last_sent(client.chat.id).text.rsplit(" ", 1)[-1]
    assert alerts(session) == [
        texts.ADMIN_DB_MARK_FAILED.format(app_id=number, error="OperationalError")
    ]
    assert session.last_sent(client.chat.id).text.startswith("Заявка принята.")


async def test_concurrent_failures_send_one_alert() -> None:
    """Два сбоя одновременно: отметка ставится до await, поэтому сообщение одно."""

    class SlowBot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, chat_id: int, text: str) -> None:
            await asyncio.sleep(0)  # отправка «в пути» — второй сбой приходит сейчас
            self.sent.append(text)

    fake = SlowBot()
    alerts_ = StorageAlerts(ADMIN_ID, clock=Clock())
    await asyncio.gather(
        alerts_.save_failed(fake, "261019-0001", "OperationalError"),  # type: ignore[arg-type]
        alerts_.save_failed(fake, "261019-0002", "OperationalError"),  # type: ignore[arg-type]
    )
    assert len(fake.sent) == 1


async def test_db_work_is_done_even_if_client_blocked_bot(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    """Ответ клиенту падает (он заблокировал бота) — отметка и уведомление уже сделаны."""
    repository = BrokenRepository(mark_error=lambda: sqlite3.OperationalError("locked"))
    client = new_client(dispatcher(make_settings(tmp_path), repository), bot, session)
    await fill_until_summary(client, session)

    def hook(method: TelegramMethod) -> None:
        if isinstance(method, SendMessage) and method.text.startswith("Заявка принята"):
            raise TelegramForbiddenError(method, "Forbidden: bot was blocked by the user")
        return None

    session.hook = hook
    with contextlib.suppress(TelegramForbiddenError):
        await client.press(SUBMIT)
    assert len(alerts(session)) == 1  # уведомление о сбое отметки ушло до ответа клиенту


async def test_no_mark_and_no_alert_when_not_delivered(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    """Telegram отказал: отмечать нечего, а уведомление о базе тоже не дойдёт — только лог."""
    repository = BrokenRepository(add_error=lambda: sqlite3.OperationalError("locked"))
    session.hook = fail_admin_with(lambda m: TelegramForbiddenError(m, "Forbidden"))
    await submit(new_client(dispatcher(make_settings(tmp_path), repository), bot, session), session)
    assert repository.marked == []
    assert len(session.sent(ADMIN_ID)) == 1  # только неудачная попытка самой заявки


# ---------- delivered_at ----------


async def test_delivered_at_set_only_after_delivery(
    tmp_path: Path, bot: Bot, session: MockSession
) -> None:
    settings = make_settings(tmp_path)
    async with SqliteRepository(settings.database_path) as repository:
        client = new_client(dispatcher(settings, repository), bot, session)
        await fill_until_summary(client, session)
        session.hook = fail_admin_with(lambda m: TelegramForbiddenError(m, "Forbidden"))
        await client.press(SUBMIT)
        (row,) = rows(settings.database_path)
        assert row["delivered_at"] is None  # заявка в базе, но не доставлена

        session.hook = None
        await client.press(SUBMIT)  # повтор после восстановления
    (row,) = rows(settings.database_path)
    delivered = datetime.strptime(row["delivered_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - delivered) < timedelta(minutes=1)
    assert row["delivered_at"] >= row["created_at"]


async def test_id_collision_is_reported_and_other_row_not_marked(
    tmp_path: Path, bot: Bot, session: MockSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Номер совпал с чужой НЕдоставленной заявкой: её отметка не ставится, админ знает."""
    settings = make_settings(tmp_path)
    async with SqliteRepository(settings.database_path) as repository:
        await repository.add(make_app())  # чужая заявка, Telegram её не доставил
        monkeypatch.setattr(
            "bot.handlers.application.new_application_id", lambda now, tz: "261019-ABCD"
        )
        client = new_client(dispatcher(settings, repository), bot, session)
        await submit(client, session)
        assert await repository.get_delivered_at("261019-ABCD") is None
    assert alerts(session) == [
        texts.ADMIN_DB_SAVE_FAILED.format(app_id="261019-ABCD", error="ApplicationIdTakenError")
    ]
    assert session.last_sent(client.chat.id).text.startswith("Заявка принята.")
    (row,) = rows(settings.database_path)
    assert row["applicant_user_id"] == make_app().applicant.user_id  # чужая строка цела


async def test_resubmit_does_not_reset_delivered_at(tmp_path: Path) -> None:
    async with SqliteRepository(tmp_path / "bot.db") as repository:
        app = make_app()
        first = datetime(2026, 10, 19, 12, 16, tzinfo=UTC)
        await repository.add(app)
        await repository.mark_delivered(app.id, first)
        await repository.add(app.model_copy(update={"task": "Поправленная задача клиента"}))
        assert await repository.get_delivered_at(app.id) == first
        # Повторная отметка не сдвигает время первой доставки
        await repository.mark_delivered(app.id, first + timedelta(hours=1))
        assert await repository.get_delivered_at(app.id) == first
        # Время переводится в UTC, наивное — ошибка
        other = make_app("261019-0002")
        await repository.add(other)
        local = datetime(2026, 10, 20, 3, 0, tzinfo=datetime.now().astimezone().tzinfo)
        await repository.mark_delivered(other.id, local)
        assert await repository.get_delivered_at(other.id) == local
        with pytest.raises(ValueError, match="timezone-aware"):
            await repository.mark_delivered(other.id, datetime(2026, 1, 1))
        # Нет такой заявки — тихо (WARNING), без исключения
        await repository.mark_delivered("000000-0000", first)


async def test_delivered_at_check_rejects_bad_format(tmp_path: Path) -> None:
    async with SqliteRepository(tmp_path / "bot.db") as repository:
        await repository.add(make_app())
    with connect(tmp_path / "bot.db") as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE applications SET delivered_at = '19.10.2026 12:00'")


# ---------- миграция v1 → v2 ----------


async def test_migrates_v1_file_with_data(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    with connect(db) as conn:  # база, созданная ботом версии этапа 2а (схема v1)
        conn.executescript(sqlite_repository.MIGRATIONS[0] + "\nPRAGMA user_version = 1;")
        conn.execute(
            "INSERT INTO applications VALUES ('261018-AAAA', '2026-10-18T09:00:00Z', 'bot',"
            " 'Олег', 'oleg@mail.ru', 'shop', 'Магазин запчастей', 1, 42, NULL, 'Олег')"
        )
    db.chmod(0o640)

    async with SqliteRepository(db) as repository:
        status = await repository.status()
        assert (status.schema_version, status.applications) == (3, 1)  # v1 → v2 → v3
        old = await repository.get("261018-AAAA")
        assert old is not None and old.application.task == "Магазин запчастей"
        assert await repository.get_delivered_at("261018-AAAA") is None  # старым — NULL
        moment = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
        await repository.mark_delivered("261018-AAAA", moment)
        await repository.add(make_app())
    (old_row, new_row) = rows(db)
    assert old_row["delivered_at"] == "2026-10-19T10:00:00Z"
    assert new_row["id"] == "261019-ABCD"
    assert mode(db) == 0o640  # права существующего файла не тронуты


async def test_new_database_is_created_at_latest_version(tmp_path: Path) -> None:
    async with SqliteRepository(tmp_path / "bot.db") as repository:
        assert (await repository.status()).schema_version == len(sqlite_repository.MIGRATIONS) == 3
