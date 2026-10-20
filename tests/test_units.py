"""Модульные тесты: настройки, очистка ввода, лимиты, логи, форматирование."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bot.config import ConfigError, Settings, load_settings
from bot.logging_setup import RedactingFormatter
from bot.models import Applicant, Application, ProjectType, Source, new_application_id
from bot.services.formatting import format_admin_application, format_local_time
from bot.services.rate_limit import SlidingWindowLimiter
from bot.services.validation import (
    InvalidInputError,
    Limits,
    sanitize,
    validate_contact,
    validate_name,
    validate_task,
)
from tests.conftest import FAKE_TOKEN

LIMITS = Limits(name_min=2, name_max=80, contact_max=120, task_min=10, task_max=1000)


# ---------- настройки ----------


def test_missing_token_has_russian_message() -> None:
    with pytest.raises(ConfigError) as err:
        load_settings(_env_file=None)
    assert "Не найден BOT_TOKEN: скопируйте .env.example в .env и заполните" in str(err.value)
    assert "Не найден ADMIN_ID" in str(err.value)


def test_empty_values_count_as_missing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=\nADMIN_ID=\nPOLICY_URL=\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Не найден BOT_TOKEN"):
        load_settings(_env_file=env)


def test_bad_token_is_not_echoed() -> None:
    with pytest.raises(ConfigError) as err:
        load_settings(_env_file=None, bot_token="secret value with spaces", admin_id=1)
    assert "secret value" not in str(err.value)
    assert "BOT_TOKEN в .env заполнен неверно" in str(err.value)


def test_token_hidden_in_repr_and_policy_optional() -> None:
    settings = load_settings(_env_file=None, bot_token=FAKE_TOKEN, admin_id=1, policy_url="  ")
    assert FAKE_TOKEN not in repr(settings)
    assert FAKE_TOKEN not in str(settings.model_dump())
    assert settings.policy_url is None
    assert settings.timezone == "Asia/Yekaterinburg"
    assert settings.applications_per_hour == 3


def test_invalid_timezone_and_policy() -> None:
    with pytest.raises(ConfigError) as err:
        load_settings(
            _env_file=None,
            bot_token=FAKE_TOKEN,
            admin_id=1,
            timezone="Europe/Chelyabinsk",
            policy_url="javascript:alert(1)",
        )
    assert "TIMEZONE" in str(err.value) and "POLICY_URL" in str(err.value)


@pytest.mark.parametrize("raw", ["1,3", "[1, 3", "abc"])
def test_retry_delays_not_json_is_config_error(
    raw: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ошибку разбора JSON pydantic-settings бросает SettingsError, а не ValidationError:
    # без перехвата `python -m bot` падал бы с трейсбеком вместо русской подсказки.
    env = tmp_path / ".env"
    env.write_text(f"BOT_TOKEN={FAKE_TOKEN}\nADMIN_ID=1\nSEND_RETRY_DELAYS={raw}\n")
    with pytest.raises(ConfigError, match=r"SEND_RETRY_DELAYS.*\[1, 3\]"):
        load_settings(_env_file=env)
    monkeypatch.setenv("SEND_RETRY_DELAYS", raw)  # из окружения — то же самое
    with pytest.raises(ConfigError, match="SEND_RETRY_DELAYS"):
        load_settings(_env_file=None, bot_token=FAKE_TOKEN, admin_id=1)


def test_env_example_uncommented_gives_defaults(tmp_path) -> None:
    # Раскомментированный .env.example (с фиктивными токеном и ID) = значения по умолчанию.
    example = (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8")
    lines = [re.sub(r"^# ([A-Z_]+=)", r"\1", line) for line in example.splitlines()]
    env = tmp_path / ".env"
    env.write_text("\n".join([*lines, f"BOT_TOKEN={FAKE_TOKEN}", "ADMIN_ID=1"]), encoding="utf-8")
    loaded = load_settings(_env_file=env)
    defaults = load_settings(_env_file=None, bot_token=FAKE_TOKEN, admin_id=1)
    assert loaded.send_retry_delays == (1.0, 3.0)
    for field in Settings.model_fields:
        if field not in {"bot_token", "fallback_contact"}:  # контакт в примере задан
            assert getattr(loaded, field) == getattr(defaults, field), field


# ---------- очистка и проверка ----------


def test_sanitize_removes_invisible_and_collapses_spaces() -> None:
    raw = "‮Анна​ \t  Иванова\x00\x07﻿"
    assert sanitize(raw) == "Анна Иванова"
    assert sanitize("строка 1\r\nстрока 2") == "строка 1 строка 2"
    multi = sanitize("  a  \n\n\n\n  b c ", multiline=True)
    assert multi == "a\n\nb\nc"


def test_validate_contact_variants() -> None:
    assert validate_contact("Name@Mail.ru", LIMITS) == "Name@Mail.ru"
    assert validate_contact("anna_k", LIMITS) == "@anna_k"
    assert validate_contact("@anna_k", LIMITS) == "@anna_k"
    assert validate_contact("t.me/anna_k", LIMITS) == "@anna_k"
    assert validate_contact("https://t.me/anna_k/", LIMITS) == "@anna_k"
    for bad in ("", "abc", "@ab", "anna@", "<x>@y.ru", "http://evil.ru/anna_k", "a" * 121):
        with pytest.raises(InvalidInputError):
            validate_contact(bad, LIMITS)


def test_validate_lengths() -> None:
    with pytest.raises(InvalidInputError):
        validate_name("   ", LIMITS)
    assert validate_name("Ян", LIMITS) == "Ян"
    assert validate_task("x" * 1000, LIMITS) == "x" * 1000
    with pytest.raises(InvalidInputError, match="1001 символ при лимите 1000 символов"):
        validate_task("x" * 1001, LIMITS)
    with pytest.raises(InvalidInputError):
        validate_task("​​​", LIMITS)


# ---------- лимиты ----------


def test_sliding_window_limiter() -> None:
    now = [0.0]
    limiter = SlidingWindowLimiter(limit=3, window=3600, clock=lambda: now[0])
    for _ in range(3):
        assert limiter.retry_after(1) == 0
        limiter.hit(1)
    assert limiter.retry_after(1) == pytest.approx(3600)
    assert limiter.retry_after(2) == 0  # у другого пользователя свой счётчик
    now[0] = 1800
    assert limiter.retry_after(1) == pytest.approx(1800)
    now[0] = 3600.5
    assert limiter.retry_after(1) == 0


# ---------- логи ----------


def test_redacting_formatter_hides_token() -> None:
    formatter = RedactingFormatter("%(message)s", [FAKE_TOKEN])
    record = logging.LogRecord(
        "x",
        logging.ERROR,
        __file__,
        1,
        "GET https://api.telegram.org/bot%s/getMe",
        (FAKE_TOKEN,),
        None,
    )
    assert FAKE_TOKEN not in formatter.format(record)
    assert "bot***/getMe" in formatter.format(record)


# ---------- форматирование ----------


def test_admin_message_uses_configured_timezone() -> None:
    from zoneinfo import ZoneInfo

    moment = datetime(2026, 10, 17, 7, 30, tzinfo=UTC)
    assert format_local_time(moment, ZoneInfo("Asia/Yekaterinburg")) == "17.10.2026 12:30"
    app = Application(
        id=new_application_id(moment, ZoneInfo("Asia/Yekaterinburg")),
        created_at=moment,
        source=Source.SITE,
        name="Анна",
        contact="a@b.ru",
        project_type=ProjectType.SHOP,
        task="Магазин одежды",
        applicant=Applicant(user_id=1, username="anna", full_name="Анна"),
    )
    text = format_admin_application(app, ZoneInfo("Asia/Yekaterinburg"))
    assert "17.10.2026 12:30 · источник: сайт" in text
    assert "Интернет-магазин" in text
    assert app.id.startswith("261017-") and len(app.id) == 11


# ---------- запуск ----------


@pytest.mark.parametrize(
    ("failing_method", "error", "expected"),
    [
        ("GetMe", "unauthorized", "Telegram отклонил токен"),
        ("DeleteWebhook", "network", "Нет связи с Telegram"),
        ("GetMe", "server", "Telegram ответил ошибкой"),
    ],
)
async def test_startup_errors_are_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_method: str,
    error: str,
    expected: str,
) -> None:
    from aiogram.exceptions import (
        TelegramNetworkError,
        TelegramServerError,
        TelegramUnauthorizedError,
    )

    import bot.__main__ as entry
    from bot.app import create_bot
    from tests.conftest import MockSession

    errors = {
        "unauthorized": TelegramUnauthorizedError,
        "network": TelegramNetworkError,
        "server": TelegramServerError,
    }
    session = MockSession()

    def hook(method):  # type: ignore[no-untyped-def]
        if type(method).__name__ == failing_method:
            raise errors[error](method, "boom")
        return None

    session.hook = hook
    monkeypatch.setattr(entry, "create_bot", lambda s: create_bot(s, session=session))
    settings = load_settings(_env_file=None, bot_token=FAKE_TOKEN, admin_id=1)
    assert await entry.run(settings) == 1
    assert expected in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_application_id_uses_local_date() -> None:
    from zoneinfo import ZoneInfo

    chelyabinsk = ZoneInfo("Asia/Yekaterinburg")  # UTC+5
    # 18:59 UTC = 23:59 по Челябинску — ещё 19 октября
    assert new_application_id(datetime(2026, 10, 19, 18, 59, tzinfo=UTC), chelyabinsk)[:6] == (
        "261019"
    )
    # 19:00 UTC = 00:00 по Челябинску — уже 20-е, хотя в UTC ещё 19-е
    assert new_application_id(datetime(2026, 10, 19, 19, 0, tzinfo=UTC), chelyabinsk)[:6] == (
        "261020"
    )
    # 23:59 UTC = 04:59 следующего дня по Челябинску
    assert new_application_id(datetime(2026, 10, 19, 23, 59, tzinfo=UTC), chelyabinsk)[:6] == (
        "261020"
    )
    assert new_application_id(datetime(2026, 10, 19, 23, 59, tzinfo=UTC), ZoneInfo("UTC"))[:6] == (
        "261019"
    )
