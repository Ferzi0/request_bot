"""Хранилище заявок в SQLite (файл data/bot.db).

Почему стандартный sqlite3, а не aiosqlite. Модуль sqlite3 блокирующий, а бот
асинхронный: долгий вызов в цикле событий заморозил бы всех пользователей.
Поэтому все обращения к базе идут в ОДНОМ отдельном потоке (ThreadPoolExecutor
с max_workers=1): соединение создаётся и используется только в нём, запросы
выполняются строго по очереди. aiosqlite устроен так же, но это лишняя
зависимость ради нескольких записей в день.

Схема версионируется через PRAGMA user_version: при старте недостающие
миграции из MIGRATIONS применяются по порядку, каждая в своей транзакции.
Чтобы добавить столбец позже — допишите новую строку в конец MIGRATIONS,
старые не меняйте.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from bot.models import (
    Applicant,
    Application,
    ApplicationRecord,
    ApplicationStatus,
    ProjectType,
    Source,
    can_transition,
)
from bot.services.storage import DatabaseStatus, StatusChange, StatusOutcome

logger = logging.getLogger(__name__)

# Время храним в UTC, ISO 8601 с суффиксом Z: «2026-10-19T07:15:03Z».
# - UTC не зависит от пояса Mac или будущего хостинга и от перехода на летнее время;
# - строки одного формата сортируются как время, поэтому индекс работает и для
#   «старше года»: created_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 year');
# - местное время в DBeaver: datetime(created_at, 'localtime') или '+5 hours'.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Сколько ждать, если база занята другим соединением (например, DBeaver пишет).
BUSY_TIMEOUT_MS = 5000

# Миграции: индекс + 1 = номер версии схемы. Комментарии внутри CREATE TABLE
# SQLite сохраняет в sqlite_master, поэтому DBeaver покажет их в DDL таблицы.
MIGRATIONS: tuple[str, ...] = (
    # --- версия 1: таблица заявок ---
    """
    CREATE TABLE applications (
        -- Номер заявки, тот же, что видят клиент и администратор: ГГММДД-XXXX.
        id                  TEXT    NOT NULL PRIMARY KEY,
        -- Когда отправлена (первая попытка), UTC, ISO 8601: 2026-10-19T07:15:03Z.
        created_at          TEXT    NOT NULL
            CHECK (created_at GLOB
                '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
        -- Откуда пришёл человек: site — по ссылке с сайта, bot — сам нашёл бота.
        source              TEXT    NOT NULL CHECK (source IN ('site', 'bot')),
        -- Как обращаться (2–80 символов).
        name                TEXT    NOT NULL,
        -- Почта или @ник для ответа (до 120 символов).
        contact             TEXT    NOT NULL,
        -- Тип проекта: site = Сайт-визитка, landing = Лендинг, bot = Telegram-бот,
        -- service = Веб-сервис, shop = Интернет-магазин, other = Другое.
        project_type        TEXT    NOT NULL
            CHECK (project_type IN ('site', 'landing', 'bot', 'service', 'shop', 'other')),
        -- Задача своими словами (10–1000 символов).
        task                TEXT    NOT NULL,
        -- Согласие на обработку данных. Без него заявку не отправить, поэтому всегда 1;
        -- хранится явно, чтобы по строке было видно: согласие было дано.
        consent             INTEGER NOT NULL CHECK (consent = 1),
        -- Telegram-отправитель: числовой id (по нему удаляют данные по запросу),
        -- @ник без «@» (NULL, если ника нет) и имя из профиля.
        applicant_user_id   INTEGER NOT NULL,
        applicant_username  TEXT,
        applicant_full_name TEXT    NOT NULL
    ) STRICT;

    -- Для срока хранения: найти и удалить заявки старше года.
    CREATE INDEX applications_created_at_idx ON applications (created_at);
    -- Для запросов «удалите мои данные»: все заявки одного человека.
    CREATE INDEX applications_applicant_user_id_idx ON applications (applicant_user_id);
    """,
    # --- версия 2: отметка о доставке администратору ---
    # Комментарий стоит ВНУТРИ определения столбца: при ADD COLUMN SQLite вписывает
    # в DDL таблицы только текст определения, комментарий перед ним потерялся бы.
    # CHECK в ADD COLUMN допустим: у старых строк NULL, условие для них выполняется.
    """
    ALTER TABLE applications ADD COLUMN delivered_at TEXT
        -- Когда заявка дошла до администратора в Telegram, UTC, как created_at.
        -- NULL — не доставлена (Telegram отказал, клиенту показан запасной контакт).
        CHECK (delivered_at IS NULL OR delivered_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');
    """,
    # --- версия 3: статусы заявки (работа администратора) ---
    # DEFAULT 'new' — все уже сохранённые заявки становятся новыми (решение заказчика).
    # NOT NULL с константным DEFAULT в ADD COLUMN SQLite разрешает.
    # Один составной индекс (status, created_at) закрывает и фильтр по статусу,
    # и сортировку «новые сверху» в списке панели; отдельный индекс по status
    # был бы его префиксом, то есть лишним.
    """
    ALTER TABLE applications ADD COLUMN status TEXT NOT NULL DEFAULT 'new'
        -- Статус: new — новая, in_work — в работе, done — выполнена,
        -- cancelled — отменена (строка не удаляется). Меняется кнопками бота.
        CHECK (status IN ('new', 'in_work', 'done', 'cancelled'));
    ALTER TABLE applications ADD COLUMN taken_at TEXT
        -- Когда взята в работу (первое нажатие «В работу»), UTC; NULL — ещё новая.
        CHECK (taken_at IS NULL OR taken_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');
    ALTER TABLE applications ADD COLUMN closed_at TEXT
        -- Когда закрыта (выполнена или отменена), UTC; NULL — не закрыта.
        CHECK (closed_at IS NULL OR closed_at GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z');

    -- Для списков админ-панели: заявки одного статуса, новые сверху.
    CREATE INDEX applications_status_created_at_idx ON applications (status, created_at);
    """,
)

# ON CONFLICT: повтор «Отправить» после сбоя доставки приходит с тем же номером.
# Дубля не будет: строка обновляется до последней отправленной версии (человек мог
# поправить поле через «Изменить»), а created_at остаётся временем первой попытки.
# WHERE: номер случайный, и если вдруг совпадёт с чужой заявкой — её не трогаем
# (rowcount = 0 → ApplicationIdTakenError).
# delivered_at в SET нет намеренно: повтор не сбрасывает отметку о доставке.
_UPSERT = """
INSERT INTO applications (
    id, created_at, source, name, contact, project_type, task, consent,
    applicant_user_id, applicant_username, applicant_full_name
) VALUES (
    :id, :created_at, :source, :name, :contact, :project_type, :task, 1,
    :applicant_user_id, :applicant_username, :applicant_full_name
)
ON CONFLICT (id) DO UPDATE SET
    source = excluded.source,
    name = excluded.name,
    contact = excluded.contact,
    project_type = excluded.project_type,
    task = excluded.task,
    applicant_username = excluded.applicant_username,
    applicant_full_name = excluded.applicant_full_name
WHERE applications.applicant_user_id = excluded.applicant_user_id
"""

# COALESCE: отметка ставится один раз — повторный вызов не сдвигает время доставки.
_MARK_DELIVERED = """
UPDATE applications
SET delivered_at = COALESCE(delivered_at, :delivered_at)
WHERE id = :id
"""

_COLUMNS = """
    id, created_at, source, name, contact, project_type, task,
    applicant_user_id, applicant_username, applicant_full_name,
    delivered_at, status, taken_at, closed_at
"""

_SELECT_ONE = f"SELECT {_COLUMNS} FROM applications WHERE id = ?"  # noqa: S608 — константа

# Два отдельных запроса, а не «WHERE (:status IS NULL OR status = :status)»:
# с OR SQLite не может использовать индекс (status, created_at).
_LIST_ALL = f"""
SELECT {_COLUMNS} FROM applications
ORDER BY created_at DESC, id DESC
LIMIT :limit OFFSET :offset
"""  # noqa: S608 — подставляется только константа _COLUMNS
_LIST_BY_STATUS = f"""
SELECT {_COLUMNS} FROM applications
WHERE status = :status
ORDER BY created_at DESC, id DESC
LIMIT :limit OFFSET :offset
"""  # noqa: S608 — подставляется только константа _COLUMNS

# Смена статуса. «WHERE status = :expected» — защита от гонки: если между чтением
# и записью статус успел поменяться (другое нажатие, правка в DBeaver), UPDATE
# не тронет строку (rowcount = 0) и решение принимается заново по свежему статусу.
# Время: в работу — taken_at = сейчас; закрытие — closed_at = сейчас (taken_at
# остаётся); «Вернуть в новые» — оба сбрасываются.
_SET_STATUS = """
UPDATE applications SET
    status = :status,
    taken_at = CASE :status
        WHEN 'new' THEN NULL
        WHEN 'in_work' THEN :now
        ELSE taken_at
    END,
    closed_at = CASE WHEN :status IN ('done', 'cancelled') THEN :now ELSE NULL END
WHERE id = :id AND status = :expected
"""


class SchemaError(RuntimeError):
    """База создана более новой версией бота — старый код её не понимает."""


class ApplicationIdTakenError(RuntimeError):
    """Случайный номер совпал с заявкой другого человека: новая заявка не записана.

    Именно исключение, а не тихий возврат: хендлер должен узнать, что заявки в базе
    нет, — сообщить администратору и НЕ ставить отметку о доставке (по этому номеру
    она легла бы на чужую строку).
    """


class SqliteRepository:
    """Реализует ApplicationRepository поверх SQLite.

    Использование:
        async with SqliteRepository(Path("data/bot.db")) as repo:
            await repo.add(application)
    или явно open()/close() — так сделано в bot/__main__.py.
    """

    def __init__(self, path: Path | str) -> None:
        # ":memory:" — база в памяти (для тестов), иначе путь к файлу.
        self._path = path if path == ":memory:" else Path(path)
        self._executor: ThreadPoolExecutor | None = None
        self._conn: sqlite3.Connection | None = None

    # ---------- жизненный цикл ----------

    async def open(self) -> None:
        if self._executor is not None:
            raise RuntimeError("SqliteRepository уже открыт")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
        try:
            await self._run(self._open_sync)
        except BaseException:
            self._executor.shutdown(wait=True)
            self._executor = None
            raise

    async def close(self) -> None:
        if self._executor is None:
            return
        try:
            await self._run(self._close_sync)
        finally:
            self._executor.shutdown(wait=True)
            self._executor = None

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ---------- операции ----------

    @property
    def available(self) -> bool:
        return True

    async def add(self, application: Application) -> None:
        await self._run(self._add_sync, application)

    async def mark_delivered(self, app_id: str, delivered_at: datetime) -> None:
        await self._run(self._mark_delivered_sync, app_id, delivered_at)

    async def status(self) -> DatabaseStatus:
        """Версия схемы и число заявок — для сообщения администратору при старте."""
        return await self._run(self._status_sync)

    async def get_delivered_at(self, app_id: str) -> datetime | None:
        """Время доставки по номеру (None — не доставлена). Для тестов и отладки."""
        return await self._run(self._get_delivered_at_sync, app_id)

    async def get(self, app_id: str) -> ApplicationRecord | None:
        """Заявка по номеру вместе со статусом (карточка в админ-панели)."""
        return await self._run(self._get_sync, app_id)

    async def list_by_status(
        self, status: ApplicationStatus | None, limit: int, offset: int
    ) -> list[ApplicationRecord]:
        return await self._run(self._list_sync, status, limit, offset)

    async def count_by_status(self) -> dict[ApplicationStatus, int]:
        return await self._run(self._count_sync)

    async def set_status(
        self, app_id: str, status: ApplicationStatus, now: datetime
    ) -> StatusChange:
        return await self._run(self._set_status_sync, app_id, status, now)

    # ---------- синхронная часть: выполняется только в потоке базы ----------

    def _open_sync(self) -> None:
        if isinstance(self._path, Path):
            # 0o700: в базе персональные данные — папка только для владельца.
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # autocommit=True: каждый запрос — сам себе транзакция, а там, где нужно
        # несколько запросов сразу (миграции), пишем BEGIN/COMMIT явно.
        # Иначе модуль sqlite3 сам открывает транзакции в неочевидные моменты.
        is_new_file = isinstance(self._path, Path) and not self._path.exists()
        conn = sqlite3.connect(self._path, autocommit=True)
        conn.row_factory = sqlite3.Row  # строки как словари: row["name"], а не row[3]
        try:
            if is_new_file and isinstance(self._path, Path):
                # 600 — только при создании: права существующего файла выбрал его
                # владелец (или DATABASE_PATH указан по ошибке) — их не трогаем.
                # -wal и -shm SQLite создаёт с теми же правами, что и сам файл.
                self._path.chmod(0o600)
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            # WAL: читатели (DBeaver) не блокируют запись бота и наоборот.
            # Режим запоминается в самом файле базы.
            conn.execute("PRAGMA journal_mode = WAL")
            # foreign_keys не включаем: внешних ключей в схеме нет (одна таблица).
            _migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn

    def _close_sync(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _add_sync(self, app: Application) -> None:
        cursor = self._connection().execute(_UPSERT, _to_row(app))
        if cursor.rowcount == 0:
            # Совпал случайный номер с заявкой ДРУГОГО человека: чужую строку не трогаем.
            # В тексте исключения — только номер, без данных заявки.
            raise ApplicationIdTakenError(f"Номер {app.id} уже занят другой заявкой")

    def _mark_delivered_sync(self, app_id: str, delivered_at: datetime) -> None:
        params = {"id": app_id, "delivered_at": _utc_text(delivered_at)}
        if self._connection().execute(_MARK_DELIVERED, params).rowcount == 0:
            # Строки нет — значит, не удалась запись самой заявки (об этом уже есть ERROR).
            logger.warning("Заявка %s: нет в базе, отметку о доставке ставить некуда", app_id)

    def _status_sync(self) -> DatabaseStatus:
        conn = self._connection()
        (count,) = conn.execute("SELECT count(*) FROM applications").fetchone()
        return DatabaseStatus(schema_version=_schema_version(conn), applications=int(count))

    def _get_delivered_at_sync(self, app_id: str) -> datetime | None:
        sql = "SELECT delivered_at FROM applications WHERE id = ?"
        row = self._connection().execute(sql, (app_id,)).fetchone()
        return None if row is None else _parse_time(row["delivered_at"])

    def _get_sync(self, app_id: str) -> ApplicationRecord | None:
        row = self._connection().execute(_SELECT_ONE, (app_id,)).fetchone()
        return None if row is None else _record_from_row(row)

    def _list_sync(
        self, status: ApplicationStatus | None, limit: int, offset: int
    ) -> list[ApplicationRecord]:
        if limit < 0 or offset < 0:
            raise ValueError("limit and offset must be >= 0")
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if status is None:
            cursor = self._connection().execute(_LIST_ALL, params)
        else:
            cursor = self._connection().execute(_LIST_BY_STATUS, {**params, "status": status})
        return [_record_from_row(row) for row in cursor]

    def _count_sync(self) -> dict[ApplicationStatus, int]:
        sql = "SELECT status, count(*) AS n FROM applications GROUP BY status"
        counts = dict.fromkeys(ApplicationStatus, 0)
        for row in self._connection().execute(sql):
            counts[ApplicationStatus(row["status"])] = int(row["n"])
        return counts

    def _set_status_sync(
        self, app_id: str, status: ApplicationStatus, now: datetime
    ) -> StatusChange:
        params = {"id": app_id, "status": status.value, "now": _utc_text(now)}
        # Цикл — на случай, если статус поменяли извне между чтением и UPDATE.
        # Больше пары оборотов не бывает: каждый оборот видит уже свежий статус.
        for _ in range(3):
            record = self._get_sync(app_id)
            if record is None:
                return StatusChange(StatusOutcome.NOT_FOUND)
            if record.status is status:
                # Повтор того же перехода: время не трогаем, это не ошибка.
                return StatusChange(StatusOutcome.ALREADY, record)
            if not can_transition(record.status, status):
                return StatusChange(StatusOutcome.NOT_ALLOWED, record)
            cursor = self._connection().execute(
                _SET_STATUS, {**params, "expected": record.status.value}
            )
            if cursor.rowcount == 1:
                return StatusChange(StatusOutcome.CHANGED, self._get_sync(app_id))
        raise RuntimeError(f"Заявка {app_id}: статус меняется слишком часто")

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteRepository не открыт: вызовите open()")
        return self._conn

    async def _run[**P, R](self, func: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
        if self._executor is None:
            raise RuntimeError("SqliteRepository не открыт: вызовите open()")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: func(*args, **kwargs))


def _migrate(conn: sqlite3.Connection) -> None:
    """Доводит схему до последней версии. Каждая миграция — одна транзакция."""
    target = len(MIGRATIONS)
    while (current := _schema_version(conn)) != target:
        if current > target:
            raise SchemaError(
                f"Версия схемы базы {current}, а этот код знает только до {target}: "
                "база от более новой версии бота."
            )
        # BEGIN IMMEDIATE сразу берёт блокировку записи: два процесса бота
        # не выполнят одну и ту же миграцию одновременно.
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Перечитываем версию уже под блокировкой: пока мы её ждали, другая копия
            # бота могла применить эту миграцию — повторный CREATE TABLE упал бы.
            applied = _schema_version(conn) == current
            if applied:
                # executescript — несколько команд за раз; f-строка безопасна:
                # подставляется наше целое число, а не ввод пользователя.
                conn.executescript(MIGRATIONS[current] + f"\nPRAGMA user_version = {current + 1};")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if applied:
            logger.info("База данных: схема обновлена до версии %d", current + 1)


def _schema_version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return int(version)


def _utc_text(moment: datetime) -> str:
    if moment.tzinfo is None:
        # Наивное время неоднозначно (чей пояс?) — лучше явная ошибка, чем сдвиг на часы.
        raise ValueError("datetime must be timezone-aware")
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def _to_row(app: Application) -> dict[str, object]:
    return {
        "id": app.id,
        "created_at": _utc_text(app.created_at),
        "source": app.source.value,
        "name": app.name,
        "contact": app.contact,
        "project_type": app.project_type.value,
        "task": app.task,
        "applicant_user_id": app.applicant.user_id,
        "applicant_username": app.applicant.username,
        "applicant_full_name": app.applicant.full_name,
    }


def _from_row(row: sqlite3.Row) -> Application:
    # Значения из базы pydantic проверит при создании модели (типы, enum-ы).
    return Application(
        id=row["id"],
        created_at=datetime.strptime(row["created_at"], TIME_FORMAT).replace(tzinfo=UTC),
        source=Source(row["source"]),
        name=row["name"],
        contact=row["contact"],
        project_type=ProjectType(row["project_type"]),
        task=row["task"],
        applicant=Applicant(
            user_id=row["applicant_user_id"],
            username=row["applicant_username"],
            full_name=row["applicant_full_name"],
        ),
    )


def _parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)


def _record_from_row(row: sqlite3.Row) -> ApplicationRecord:
    return ApplicationRecord(
        application=_from_row(row),
        status=ApplicationStatus(row["status"]),
        delivered_at=_parse_time(row["delivered_at"]),
        taken_at=_parse_time(row["taken_at"]),
        closed_at=_parse_time(row["closed_at"]),
    )
