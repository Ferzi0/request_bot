"""Статусы заявок в базе: миграция v2 → v3, переходы, время, гонки, выборки.

Все базы — во временных папках pytest; data/ проекта тесты не трогают.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.models import ALLOWED_TRANSITIONS, ApplicationRecord, ApplicationStatus
from bot.services import sqlite_repository
from bot.services.sqlite_repository import SqliteRepository
from bot.services.storage import NullRepository, StatusOutcome
from tests.test_storage import connect, make_app, mode, rows

NEW, IN_WORK, DONE, CANCELLED = (
    ApplicationStatus.NEW,
    ApplicationStatus.IN_WORK,
    ApplicationStatus.DONE,
    ApplicationStatus.CANCELLED,
)
T0 = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[SqliteRepository]:
    async with SqliteRepository(tmp_path / "bot.db") as repository:
        yield repository


async def bring_to(repo: SqliteRepository, app_id: str, status: ApplicationStatus) -> None:
    """Заявка в нужном статусе — только допустимыми переходами."""
    path = {
        NEW: [],
        IN_WORK: [IN_WORK],
        DONE: [IN_WORK, DONE],
        CANCELLED: [CANCELLED],
    }[status]
    for step in path:
        assert (await repo.set_status(app_id, step, T0)).outcome is StatusOutcome.CHANGED


async def record(repo: SqliteRepository, app_id: str = "261019-ABCD") -> ApplicationRecord:
    found = await repo.get(app_id)
    assert found is not None
    return found


# ---------- миграция ----------


async def test_migrates_v2_file_with_data(tmp_path: Path) -> None:
    db = tmp_path / "bot.db"
    with connect(db) as conn:  # база этапа 2а: схема v2, заявка доставлена
        conn.executescript(
            sqlite_repository.MIGRATIONS[0]
            + sqlite_repository.MIGRATIONS[1]
            + "\nPRAGMA user_version = 2;"
        )
        conn.execute(
            "INSERT INTO applications VALUES ('261018-AAAA', '2026-10-18T09:00:00Z', 'bot',"
            " 'Олег', 'oleg@mail.ru', 'shop', 'Магазин запчастей', 1, 42, NULL, 'Олег',"
            " '2026-10-18T09:00:05Z')"
        )
    db.chmod(0o640)

    async with SqliteRepository(db) as repository:
        assert (await repository.status()).schema_version == 3
        old = await record(repository, "261018-AAAA")
        assert (old.status, old.taken_at, old.closed_at) == (NEW, None, None)
        assert old.delivered_at == datetime(2026, 10, 18, 9, 0, 5, tzinfo=UTC)
        change = await repository.set_status("261018-AAAA", IN_WORK, T0)
        assert change.outcome is StatusOutcome.CHANGED

    (row,) = rows(db)
    assert (row["status"], row["taken_at"], row["closed_at"]) == (
        "in_work",
        "2026-10-19T10:00:00Z",
        None,
    )
    assert row["task"] == "Магазин запчастей"
    with connect(db) as conn:
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'applications'").fetchone()
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(applications)")}
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM applications WHERE status = 'new' "
            "ORDER BY created_at DESC"
        ).fetchall()
    assert "-- Статус: new — новая" in ddl[0]
    assert "-- Когда взята в работу" in ddl[0] and "-- Когда закрыта" in ddl[0]
    assert "applications_status_created_at_idx" in indexes
    assert "applications_status_created_at_idx" in str([tuple(r) for r in plan])
    assert mode(db) == 0o640


async def test_status_check_rejects_unknown_value(repo: SqliteRepository, tmp_path: Path) -> None:
    import sqlite3

    await repo.add(make_app())
    with connect(tmp_path / "bot.db") as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE applications SET status = 'archived'")
    with connect(tmp_path / "bot.db") as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE applications SET taken_at = '19.10.2026'")


# ---------- переходы ----------


@pytest.mark.parametrize(
    ("current", "target"), list(itertools.product(ApplicationStatus, ApplicationStatus))
)
async def test_every_transition(
    repo: SqliteRepository, current: ApplicationStatus, target: ApplicationStatus
) -> None:
    await repo.add(make_app())
    await bring_to(repo, "261019-ABCD", current)
    change = await repo.set_status("261019-ABCD", target, T0 + timedelta(hours=1))
    if target is current:
        expected = StatusOutcome.ALREADY
    elif target in ALLOWED_TRANSITIONS[current]:
        expected = StatusOutcome.CHANGED
    else:
        expected = StatusOutcome.NOT_ALLOWED
    assert change.outcome is expected
    final = (await record(repo)).status
    assert final is (target if expected is StatusOutcome.CHANGED else current)
    assert change.record is not None and change.record.status is final


def test_transition_table_matches_customer_decision() -> None:
    assert {(a, b) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets} == {
        (NEW, IN_WORK),
        (NEW, CANCELLED),
        (IN_WORK, DONE),
        (IN_WORK, CANCELLED),
        (IN_WORK, NEW),
        (DONE, NEW),
        (CANCELLED, NEW),
    }


async def test_timestamps_are_idempotent_and_reset(repo: SqliteRepository) -> None:
    await repo.add(make_app())
    t1, t2, t3, t4 = (T0 + timedelta(minutes=m) for m in (1, 2, 3, 4))

    await repo.set_status("261019-ABCD", IN_WORK, t1)
    again = await repo.set_status("261019-ABCD", IN_WORK, t2)  # повтор «В работу»
    assert again.outcome is StatusOutcome.ALREADY
    assert again.record is not None and again.record.taken_at == t1  # время не сдвинулось

    await repo.set_status("261019-ABCD", DONE, t3)
    done_again = await repo.set_status("261019-ABCD", DONE, t4)
    assert done_again.outcome is StatusOutcome.ALREADY
    done = await record(repo)
    assert (done.taken_at, done.closed_at) == (t1, t3)  # закрытие не трогает taken_at

    await repo.set_status("261019-ABCD", NEW, t4)  # «Вернуть в новые»
    reopened = await record(repo)
    assert (reopened.status, reopened.taken_at, reopened.closed_at) == (NEW, None, None)

    await repo.set_status("261019-ABCD", CANCELLED, t4)  # отмена новой
    cancelled = await record(repo)
    assert (cancelled.taken_at, cancelled.closed_at) == (None, t4)
    assert len(await repo.list_by_status(None, 10, 0)) == 1  # не удалена

    await repo.set_status("261019-ABCD", NEW, t4)
    await repo.set_status("261019-ABCD", IN_WORK, t2)
    await repo.set_status("261019-ABCD", NEW, t3)  # из работы — тоже сброс
    assert (await record(repo)).taken_at is None


async def test_time_is_stored_in_utc_and_naive_rejected(
    repo: SqliteRepository, tmp_path: Path
) -> None:
    from zoneinfo import ZoneInfo

    await repo.add(make_app())
    local = datetime(2026, 10, 20, 3, 30, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
    await repo.set_status("261019-ABCD", IN_WORK, local)
    assert rows(tmp_path / "bot.db")[0]["taken_at"] == "2026-10-19T22:30:00Z"
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.set_status("261019-ABCD", DONE, datetime(2026, 1, 1))


async def test_unknown_and_malicious_ids(repo: SqliteRepository, tmp_path: Path) -> None:
    await repo.add(make_app())
    for app_id in ("000000-0000", "' OR 1=1 --", "261019-ABCD' OR '1'='1"):
        change = await repo.set_status(app_id, IN_WORK, T0)
        assert change.outcome is StatusOutcome.NOT_FOUND and change.record is None
        assert await repo.get(app_id) is None
    assert rows(tmp_path / "bot.db")[0]["status"] == "new"  # чужие строки не тронуты


async def test_concurrent_set_status_same_connection(repo: SqliteRepository) -> None:
    await repo.add(make_app())
    await bring_to(repo, "261019-ABCD", IN_WORK)
    results = await asyncio.gather(
        repo.set_status("261019-ABCD", DONE, T0), repo.set_status("261019-ABCD", CANCELLED, T0)
    )
    assert sorted(r.outcome for r in results) == [StatusOutcome.CHANGED, StatusOutcome.NOT_ALLOWED]


async def test_concurrent_set_status_two_connections(tmp_path: Path) -> None:
    """Две копии бота (или бот и DBeaver) меняют статус одновременно."""
    db = tmp_path / "bot.db"
    async with SqliteRepository(db) as first, SqliteRepository(db) as second:
        for i in range(20):
            app_id = f"261019-{i:04X}"
            await first.add(make_app(app_id))
            await bring_to(first, app_id, IN_WORK)
            done, cancelled = await asyncio.gather(
                first.set_status(app_id, DONE, T0), second.set_status(app_id, CANCELLED, T0)
            )
            outcomes = sorted([done.outcome, cancelled.outcome])
            assert outcomes == [StatusOutcome.CHANGED, StatusOutcome.NOT_ALLOWED], i
            winner = DONE if done.outcome is StatusOutcome.CHANGED else CANCELLED
            assert (await record(first, app_id)).status is winner


async def test_update_checks_current_status(
    repo: SqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Статус поменялся между чтением и UPDATE: устаревшее решение не применяется."""
    await repo.add(make_app())
    await bring_to(repo, "261019-ABCD", DONE)
    real_get = repo._get_sync
    stale = (await record(repo)).model_copy(update={"status": IN_WORK})
    calls = {"n": 0}

    def first_read_is_stale(app_id: str) -> ApplicationRecord | None:
        calls["n"] += 1
        return stale if calls["n"] == 1 else real_get(app_id)

    monkeypatch.setattr(repo, "_get_sync", first_read_is_stale)
    # По устаревшему чтению (in_work) отмена допустима, но на деле заявка уже done.
    change = await repo.set_status("261019-ABCD", CANCELLED, T0)
    assert change.outcome is StatusOutcome.NOT_ALLOWED
    assert (await record(repo)).status is DONE


# ---------- выборки ----------


async def test_list_and_count(repo: SqliteRepository) -> None:
    assert await repo.count_by_status() == dict.fromkeys(ApplicationStatus, 0)
    assert await repo.list_by_status(None, 5, 0) == []
    for i in range(7):
        await repo.add(make_app(f"261019-{i:04X}", created_at=T0 + timedelta(minutes=i)))
    await bring_to(repo, "261019-0001", IN_WORK)
    await bring_to(repo, "261019-0002", DONE)
    await bring_to(repo, "261019-0003", CANCELLED)

    assert await repo.count_by_status() == {NEW: 4, IN_WORK: 1, DONE: 1, CANCELLED: 1}
    everything = await repo.list_by_status(None, 5, 0)
    assert [r.application.id for r in everything] == [f"261019-{i:04X}" for i in (6, 5, 4, 3, 2)]
    tail = await repo.list_by_status(None, 5, 5)
    assert [r.application.id for r in tail] == ["261019-0001", "261019-0000"]
    new = await repo.list_by_status(NEW, 5, 0)
    assert [r.application.id for r in new] == [f"261019-{i:04X}" for i in (6, 5, 4, 0)]
    assert [r.status for r in await repo.list_by_status(IN_WORK, 5, 0)] == [IN_WORK]
    assert await repo.list_by_status(DONE, 5, 1) == []
    with pytest.raises(ValueError):
        await repo.list_by_status(None, -1, 0)


async def test_null_repository_is_safe() -> None:
    null = NullRepository()
    assert not null.available
    assert await null.get("x") is None
    assert await null.list_by_status(None, 5, 0) == []
    assert await null.count_by_status() == dict.fromkeys(ApplicationStatus, 0)
    assert (await null.set_status("x", IN_WORK, T0)).outcome is StatusOutcome.NOT_FOUND
