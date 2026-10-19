"""Хранилище заявок в SQLite: схема, идемпотентность, сбои, сквозной сценарий.

Все базы — во временных папках pytest (tmp_path) или в памяти; data/ проекта
тесты не трогают.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import stat
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import User

from bot import texts
from bot.app import create_dispatcher
from bot.config import PROJECT_ROOT, ConfigError, Settings, load_settings
from bot.models import Applicant, Application, ProjectType, Source
from bot.services import sqlite_repository
from bot.services.sqlite_repository import ApplicationIdTakenError, SchemaError, SqliteRepository
from tests.conftest import ADMIN_ID, CLIENT_ID, FAKE_TOKEN, FakeUser, MockSession
from tests.test_delivery import fail_admin_with
from tests.test_flow import SUBMIT, TASK, fill_until_summary


def make_app(app_id: str = "261019-ABCD", **changes: object) -> Application:
    fields: dict[str, object] = {
        "id": app_id,
        "created_at": datetime(2026, 10, 19, 12, 15, 3, 123456, tzinfo=UTC),
        "source": Source.SITE,
        "name": "Анна",
        "contact": "anna@mail.ru",
        "project_type": ProjectType.LANDING,
        "task": TASK,
        "applicant": Applicant(user_id=CLIENT_ID, username="anna_k", full_name="Анна К"),
    }
    fields.update(changes)
    return Application.model_validate(fields)


def connect(db: Path) -> closing[sqlite3.Connection]:
    """Отдельное соединение, как у DBeaver. closing(): «with connect(...)» у sqlite3
    только завершает транзакцию, а соединение не закрывает."""
    return closing(sqlite3.connect(db, autocommit=True))


def rows(db: Path) -> list[sqlite3.Row]:
    """Читаем файл отдельным соединением — как это сделал бы DBeaver."""
    with connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM applications ORDER BY created_at, id").fetchall()


def mode(path: Path) -> int:
    """Права доступа файла, например 0o600."""
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "bot.db"  # папки data ещё нет — репозиторий создаст


@pytest.fixture
async def repo(db_path: Path) -> AsyncIterator[SqliteRepository]:
    async with SqliteRepository(db_path) as repository:
        yield repository


# ---------- схема и жизненный цикл ----------


async def test_creates_schema_on_empty_database(repo: SqliteRepository, db_path: Path) -> None:
    assert db_path.parent.is_dir()
    with connect(db_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (
            len(sqlite_repository.MIGRATIONS),
        )
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(applications)")]
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(applications)")}
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'applications'").fetchone()
    assert columns == [
        "id",
        "created_at",
        "source",
        "name",
        "contact",
        "project_type",
        "task",
        "consent",
        "applicant_user_id",
        "applicant_username",
        "applicant_full_name",
        "delivered_at",
    ]
    assert {"applications_created_at_idx", "applications_applicant_user_id_idx"} <= indexes
    assert "-- Номер заявки" in ddl[0]  # комментарии видны в DDL (DBeaver)
    assert "-- Когда заявка дошла до администратора" in ddl[0]  # и из миграции 2


async def test_database_files_are_private(repo: SqliteRepository, db_path: Path) -> None:
    await repo.add(make_app())
    wal = db_path.with_name(db_path.name + "-wal")
    assert [mode(p) for p in (db_path.parent, db_path, wal)] == [0o700, 0o600, 0o600]


async def test_reopen_keeps_data_and_does_not_migrate_again(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with SqliteRepository(db_path) as first:
        await first.add(make_app())
    assert "схема обновлена до версии 1" in caplog.text

    caplog.clear()
    async with SqliteRepository(db_path) as second:
        assert await second.get("261019-ABCD") is not None
    assert "схема обновлена" not in caplog.text
    assert len(rows(db_path)) == 1


async def test_newer_schema_is_refused(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True)
    with connect(db_path) as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaError):
        await SqliteRepository(db_path).open()


async def test_failed_migration_is_rolled_back(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = len(sqlite_repository.MIGRATIONS)
    broken = (*sqlite_repository.MIGRATIONS, "CREATE TABLE extra (x TEXT); SELECT * FROM nope;")
    monkeypatch.setattr(sqlite_repository, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        await SqliteRepository(db_path).open()
    with connect(db_path) as conn:
        # Настоящие миграции применились и зафиксированы, сломанная откатилась целиком.
        assert conn.execute("PRAGMA user_version").fetchone() == (good,)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "applications" in tables and "extra" not in tables


def test_failed_migration_leaves_no_open_transaction(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Соединение после сбоя миграции не остаётся внутри транзакции с блокировкой записи."""
    monkeypatch.setattr(
        sqlite_repository, "MIGRATIONS", ("CREATE TABLE t (x); SELECT * FROM nope;",)
    )
    db_path.parent.mkdir(parents=True)
    with connect(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            sqlite_repository._migrate(conn)
        assert not conn.in_transaction
        assert sqlite_repository._schema_version(conn) == 0


async def test_migration_already_applied_by_other_process(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Две копии бота стартуют вместе: вторая прочитала версию 0 до того, как первая
    закончила миграцию. Под блокировкой версия перечитывается — CREATE TABLE не повторяется."""
    async with SqliteRepository(db_path):
        pass  # «первая копия» уже создала схему
    real_version = sqlite_repository._schema_version
    calls = 0

    def stale_first_read(conn: sqlite3.Connection) -> int:
        nonlocal calls
        calls += 1
        return 0 if calls == 1 else real_version(conn)

    monkeypatch.setattr(sqlite_repository, "_schema_version", stale_first_read)
    async with SqliteRepository(db_path) as second:
        await second.add(make_app())
    assert calls >= 2 and len(rows(db_path)) == 1


async def test_closed_repository_raises_and_can_reopen(db_path: Path) -> None:
    repository = SqliteRepository(db_path)
    with pytest.raises(RuntimeError, match="не открыт"):
        await repository.add(make_app())
    await repository.open()
    await repository.add(make_app())
    await repository.close()
    await repository.close()  # повторное закрытие безопасно
    with pytest.raises(RuntimeError, match="не открыт"):
        await repository.get("261019-ABCD")
    await repository.open()
    assert await repository.get("261019-ABCD") is not None
    await repository.close()


async def test_in_memory_database() -> None:
    async with SqliteRepository(":memory:") as repository:
        await repository.add(make_app())
        assert await repository.get("261019-ABCD") is not None


# ---------- запись и чтение ----------


async def test_add_and_get_roundtrip(repo: SqliteRepository, db_path: Path) -> None:
    app = make_app()
    await repo.add(app)
    loaded = await repo.get(app.id)
    # Время хранится с точностью до секунды — так читаемее в DBeaver.
    assert loaded == app.model_copy(update={"created_at": app.created_at.replace(microsecond=0)})
    assert await repo.get("000000-0000") is None

    (row,) = rows(db_path)
    assert dict(row) == {
        "id": "261019-ABCD",
        "created_at": "2026-10-19T12:15:03Z",
        "source": "site",
        "name": "Анна",
        "contact": "anna@mail.ru",
        "project_type": "landing",
        "task": TASK,
        "consent": 1,
        "applicant_user_id": CLIENT_ID,
        "applicant_username": "anna_k",
        "applicant_full_name": "Анна К",
        "delivered_at": None,
    }


async def test_time_is_stored_in_utc(repo: SqliteRepository, db_path: Path) -> None:
    yekaterinburg = timezone(timedelta(hours=5))
    await repo.add(make_app(created_at=datetime(2026, 1, 1, 3, 0, tzinfo=yekaterinburg)))
    assert rows(db_path)[0]["created_at"] == "2025-12-31T22:00:00Z"


async def test_naive_time_is_rejected(repo: SqliteRepository) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.add(make_app(created_at=datetime(2026, 1, 1)))


async def test_username_may_be_missing(repo: SqliteRepository) -> None:
    app = make_app(applicant=Applicant(user_id=1, username=None, full_name="Без ника"))
    await repo.add(app)
    loaded = await repo.get(app.id)
    assert loaded is not None and loaded.applicant.username is None


async def test_same_id_does_not_duplicate(repo: SqliteRepository, db_path: Path) -> None:
    await repo.add(make_app())
    await repo.add(make_app())
    # Повтор после «Изменить»: тот же номер, поправленная задача — строка обновляется,
    # время первой попытки сохраняется.
    later = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)
    await repo.add(make_app(task="Исправленная задача: лендинг", created_at=later))
    (row,) = rows(db_path)
    assert row["task"] == "Исправленная задача: лендинг"
    assert row["created_at"] == "2026-10-19T12:15:03Z"


async def test_id_collision_with_other_user_keeps_original(
    repo: SqliteRepository, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    await repo.add(make_app())
    stranger = Applicant(user_id=777, username="evil", full_name="Чужой")
    # Исключение, а не тихий пропуск: хендлер должен знать, что заявки в базе нет.
    with pytest.raises(ApplicationIdTakenError) as error:
        await repo.add(make_app(name="Чужое имя", task="Чужая задача целиком", applicant=stranger))
    (row,) = rows(db_path)
    assert row["name"] == "Анна" and row["applicant_user_id"] == CLIENT_ID
    assert "Чужое" not in str(error.value) and "Чужое" not in caplog.text


@pytest.mark.parametrize(
    ("name", "task"),
    [
        ('O\'Brien "Кавычки"', "Задача с 'одинарными' и \"двойными\" кавычками"),
        ("'; DROP TABLE applications;--", "'); DELETE FROM applications; --"),
        ("Анна 😀👍🏽", "Эмодзи 🚀🔥 и иероглифы 漢字, а ещё \\ и % и _ и NUL-подобное \\0"),
        ("Я" * 80, ("😀&<'\"" * 200)[:1000]),
    ],
)
async def test_values_are_parameterized(
    repo: SqliteRepository, db_path: Path, name: str, task: str
) -> None:
    contact = "a" * 64 + "@" + "б" * 52 + ".ru"  # 120 символов
    app = make_app(name=name, task=task, contact=contact)
    await repo.add(app)
    loaded = await repo.get(app.id)
    assert loaded is not None
    assert (loaded.name, loaded.task, loaded.contact) == (name, task, contact)
    assert len(rows(db_path)) == 1  # таблица на месте, лишнего не удалено


async def test_parallel_writes(repo: SqliteRepository, db_path: Path) -> None:
    apps = [make_app(f"261019-{i:04X}") for i in range(50)]
    await asyncio.gather(*(repo.add(app) for app in apps))
    assert len(rows(db_path)) == 50


async def test_two_connections_write_concurrently(repo: SqliteRepository, db_path: Path) -> None:
    """Два процесса (например, бот и DBeaver) на одном файле: busy_timeout + WAL."""
    async with SqliteRepository(db_path) as other:
        first = [repo.add(make_app(f"261019-A{i:03X}")) for i in range(30)]
        second = [other.add(make_app(f"261019-B{i:03X}")) for i in range(30)]
        await asyncio.gather(*first, *second)
    assert len(rows(db_path)) == 60


async def test_readable_while_bot_connection_is_open(repo: SqliteRepository, db_path: Path) -> None:
    await repo.add(make_app())
    reader = sqlite3.connect(db_path)
    try:
        reader.execute("BEGIN")  # долгое чтение, как открытая вкладка в DBeaver
        assert reader.execute("SELECT count(*) FROM applications").fetchone() == (1,)
        await repo.add(make_app("261019-0002"))  # запись не ждёт читателя (WAL)
        reader.execute("COMMIT")
        assert reader.execute("SELECT count(*) FROM applications").fetchone() == (2,)
    finally:
        reader.close()


# ---------- интеграция с ботом ----------


class FailingRepository:
    async def add(self, application: Application) -> None:
        raise sqlite3.OperationalError(f"database is locked: {application.name}")


@pytest.fixture
def tmp_settings(tmp_path: Path) -> Settings:
    return load_settings(
        _env_file=None,
        bot_token=FAKE_TOKEN,
        admin_id=ADMIN_ID,
        send_retry_delays=(0.01,),
        notify_admin_on_start=False,
        database_path=tmp_path / "bot.db",
    )


def make_client(settings: Settings, bot: Bot, session: MockSession, repository: object) -> FakeUser:
    dp = create_dispatcher(settings, repository=repository)  # type: ignore[arg-type]
    user = User(id=CLIENT_ID, is_bot=False, first_name="Анна", last_name="К", username="anna_k")
    return FakeUser(dp, bot, session, user)


async def test_submit_writes_row_end_to_end(
    tmp_settings: Settings, bot: Bot, session: MockSession
) -> None:
    async with SqliteRepository(tmp_settings.database_path) as repository:
        client = make_client(tmp_settings, bot, session, repository)
        await fill_until_summary(client, session, start="/start site")
        await client.press(SUBMIT)

    (row,) = rows(tmp_settings.database_path)
    number = session.last_sent(CLIENT_ID).text.rsplit(" ", 1)[-1]
    assert row["id"] == number
    assert f"№ {number}" in session.last_sent(ADMIN_ID).text
    assert (row["source"], row["name"], row["contact"], row["project_type"], row["task"]) == (
        "site",
        "Анна",
        "anna@mail.ru",
        "landing",
        TASK,
    )
    assert (row["consent"], row["applicant_user_id"], row["applicant_username"]) == (
        1,
        CLIENT_ID,
        "anna_k",
    )
    assert row["applicant_full_name"] == "Анна К"
    saved = datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - saved) < timedelta(minutes=1)


async def test_failed_delivery_still_saved_once_after_retry(
    tmp_settings: Settings, bot: Bot, session: MockSession
) -> None:
    """Telegram недоступен → заявка уже в базе; повтор с тем же номером не дублирует."""
    async with SqliteRepository(tmp_settings.database_path) as repository:
        client = make_client(tmp_settings, bot, session, repository)
        await fill_until_summary(client, session)
        session.hook = fail_admin_with(lambda m: TelegramForbiddenError(m, "Forbidden"))
        await client.press(SUBMIT)
        assert len(rows(tmp_settings.database_path)) == 1
        session.hook = None
        await client.press(SUBMIT)
        assert session.last_sent(CLIENT_ID).text.startswith("Заявка принята.")
    assert len(rows(tmp_settings.database_path)) == 1


async def test_storage_failure_does_not_break_delivery(
    tmp_settings: Settings, bot: Bot, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = make_client(tmp_settings, bot, session, FailingRepository())
    await fill_until_summary(client, session)
    await client.press(SUBMIT)

    application, alert = session.sent(ADMIN_ID)  # сначала заявка, потом предупреждение
    assert TASK in application.text
    number = session.last_sent(CLIENT_ID).text.rsplit(" ", 1)[-1]
    assert alert.text == texts.ADMIN_DB_SAVE_FAILED.format(app_id=number, error="OperationalError")
    assert session.last_sent(CLIENT_ID).text.startswith("Заявка принята.")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "НЕ сохранена в базу: OperationalError" in errors[0].getMessage()
    assert errors[0].exc_info is None  # без трейсбека: в нём мог бы быть текст заявки
    for personal in ("Анна", "anna@mail.ru", TASK, "anna_k"):
        assert personal not in caplog.text


def test_database_path_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base = {"_env_file": None, "bot_token": FAKE_TOKEN, "admin_id": ADMIN_ID}
    monkeypatch.delenv("DATABASE_PATH")  # conftest подставляет временный путь — убираем
    assert load_settings(**base).database_path == PROJECT_ROOT / "data" / "bot.db"
    monkeypatch.setenv("DATABASE_PATH", "other/my.db")
    assert load_settings(**base).database_path == PROJECT_ROOT / "other" / "my.db"
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "abs.db"))
    assert load_settings(**base).database_path == tmp_path / "abs.db"
    # «DATABASE_PATH=» — как не задано (env_ignore_empty), путь по умолчанию.
    monkeypatch.setenv("DATABASE_PATH", "")
    assert load_settings(**base).database_path == PROJECT_ROOT / "data" / "bot.db"
    # Пробел или «.» — это папка проекта, а не файл: понятная ошибка настройки,
    # а не молчаливая работа без базы.
    for empty in (" ", "."):
        monkeypatch.setenv("DATABASE_PATH", empty)
        with pytest.raises(ConfigError, match="DATABASE_PATH"):
            load_settings(**base)


# ---------- точка входа ----------


async def test_run_opens_and_closes_database(
    monkeypatch: pytest.MonkeyPatch, tmp_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    import bot.__main__ as entry
    from bot.app import create_bot

    closed: list[bool] = []
    original_close = SqliteRepository.close

    async def tracking_close(self: SqliteRepository) -> None:
        closed.append(True)
        await original_close(self)

    def stop(method: object) -> None:
        raise RuntimeError("stop")  # первый же запрос к Telegram прерывает запуск

    session = MockSession()
    session.hook = stop
    monkeypatch.setattr(entry, "create_bot", lambda s: create_bot(s, session=session))
    monkeypatch.setattr(SqliteRepository, "close", tracking_close)
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError, match="stop"):
        await entry.run(tmp_settings)
    assert tmp_settings.database_path.exists()  # схема создана при старте
    assert closed == [True]  # и база закрыта в finally даже при ошибке
    assert "База заявок:" in caplog.text


async def test_run_without_database_if_it_cannot_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import bot.__main__ as entry

    blocker = tmp_path / "file"
    blocker.write_text("не папка")
    settings = load_settings(
        _env_file=None, bot_token=FAKE_TOKEN, admin_id=1, database_path=blocker / "bot.db"
    )
    repository, status = await entry.open_repository(settings)
    assert repository is None
    assert status.error == "FileExistsError" and not status.ok
    assert "Бот работает БЕЗ сохранения заявок в базу" in caplog.text
