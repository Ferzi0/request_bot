"""Настройки бота: читаются из переменных окружения и файла .env.

Почему pydantic-settings: типы и проверки описаны один раз декларативно,
а секреты хранятся как SecretStr и не утекают в repr()/логи/трейсбеки.
"""

from __future__ import annotations

import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env ищем рядом с проектом, а не в текущем каталоге: так `python -m bot`
# работает одинаково, откуда бы его ни запустили.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Формат токена @BotFather: «<цифры id бота>:<буквы, цифры, _ и ->».
# Проверяем только форму — действителен ли токен, скажет сам Telegram при запуске.
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    """Понятная человеку ошибка конфигурации (без трейсбека pydantic)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # «BOT_TOKEN=» из скопированного .env.example — это «не задано», а не пустой токен.
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    # --- обязательные ---
    bot_token: SecretStr
    admin_id: int = Field(gt=0)

    # --- тексты и контакты ---
    # Пока у сайта нет домена, ссылки на политику нет: бот работает и без неё.
    policy_url: str | None = None
    # Куда писать клиенту, если заявку не удалось переслать (почта/ник).
    fallback_contact: str | None = None

    # --- поведение ---
    timezone: str = "Asia/Yekaterinburg"  # Челябинск, UTC+5
    applications_per_hour: int = Field(default=3, ge=1)
    notify_admin_on_start: bool = True
    log_level: str = "INFO"

    # Повторы отправки администратору: паузы между попытками, секунды.
    # (1, 3) = 3 попытки: сразу, через 1 с, ещё через 3 с.
    send_retry_delays: tuple[float, ...] = (1.0, 3.0)

    # --- лимиты полей (как на сайте) ---
    name_min: int = 2
    name_max: int = 80
    contact_max: int = 120
    task_min: int = 10
    task_max: int = 1000

    @field_validator("bot_token")
    @classmethod
    def _check_token_format(cls, value: SecretStr) -> SecretStr:
        if not _TOKEN_RE.fullmatch(value.get_secret_value().strip()):
            # Сам токен в сообщение не попадает.
            raise ValueError("token format")
        return SecretStr(value.get_secret_value().strip())

    @field_validator("policy_url", "fallback_contact")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("policy_url")
    @classmethod
    def _check_policy_url(cls, value: str | None) -> str | None:
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("policy url")
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("log level")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


# Человеческие сообщения для частых ошибок. Ключ — (поле, тип ошибки pydantic | None).
_HINTS: dict[str, str] = {
    "bot_token:missing": (
        "Не найден BOT_TOKEN: скопируйте .env.example в .env и заполните (токен выдаёт @BotFather)."
    ),
    "bot_token": (
        "BOT_TOKEN в .env заполнен неверно: ожидается вид 123456789:AAE... "
        "(скопируйте токен из @BotFather целиком, без кавычек и пробелов)."
    ),
    "admin_id:missing": (
        "Не найден ADMIN_ID: скопируйте .env.example в .env и впишите свой числовой "
        "Telegram ID (узнать: @userinfobot)."
    ),
    "admin_id": "ADMIN_ID в .env должен быть положительным числом — ваш Telegram ID.",
    "policy_url": "POLICY_URL должен начинаться с https:// (или оставьте пустым).",
    "timezone": "TIMEZONE: неизвестный часовой пояс. Пример: Asia/Yekaterinburg.",
    "log_level": "LOG_LEVEL: допустимо DEBUG, INFO, WARNING, ERROR.",
}


def load_settings(**overrides: object) -> Settings:
    """Читает настройки; при ошибке бросает ConfigError с текстом на русском."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors(include_input=False):  # input мог бы содержать токен
            field = str(err["loc"][0]) if err["loc"] else "?"
            hint = _HINTS.get(f"{field}:{err['type']}") or _HINTS.get(field)
            lines.append(hint or f"Параметр {field.upper()} в .env заполнен неверно.")
        raise ConfigError("\n".join(dict.fromkeys(lines))) from None
