"""Очистка и проверка пользовательского ввода.

Правила совпадают с формой сайта (js/main.js и send.php), чтобы заявка
из бота и с сайта выглядела одинаково.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from bot import texts

# Категории Unicode, которые вырезаем:
#   Cc — управляющие (кроме перевода строки), Cf — «невидимые» форматирующие
#   (zero-width space, bidi-переключатели вроде U+202E, которыми подменяют
#   направление текста), Co/Cs/Cn — частные, суррогаты, неназначенные.
_DROP_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
_LINE_SEPARATORS = {" ", " ", "\x85", "\r"}

_SPACES_RE = re.compile(r"[^\S\n]+")  # любые пробельные, кроме \n
_MANY_NEWLINES_RE = re.compile(r"\n{3,}")

# Те же выражения, что на сайте.
_EMAIL_RE = re.compile(r'^[^\s@<>()\[\]\\,;:"]+@[^\s@<>()\[\]\\,;:"]+\.[^\s@<>()\[\]\\,;:"]{2,}$')
_TG_USERNAME_RE = re.compile(r"^@?([a-zA-Z][a-zA-Z0-9_]{4,31})$")
_TG_LINK_RE = re.compile(r"^(?:https?://)?t\.me/([a-zA-Z0-9_]{5,32})/?$", re.IGNORECASE)


def sanitize(value: str, *, multiline: bool = False) -> str:
    """Убирает управляющие/невидимые символы, схлопывает пробелы, обрезает края."""
    chars: list[str] = []
    for ch in unicodedata.normalize("NFC", value):
        if ch in _LINE_SEPARATORS:
            chars.append("\n")
        elif ch == "\n" or unicodedata.category(ch) not in _DROP_CATEGORIES:
            chars.append(ch)
    text = "".join(chars)
    if not multiline:
        text = text.replace("\n", " ")
    text = _SPACES_RE.sub(" ", text)
    if multiline:
        text = "\n".join(line.strip() for line in text.split("\n"))
        text = _MANY_NEWLINES_RE.sub("\n\n", text)
    return text.strip()


class InvalidInputError(ValueError):
    """Ввод не прошёл проверку. message — готовый текст для человека."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class Limits:
    name_min: int
    name_max: int
    contact_max: int
    task_min: int
    task_max: int


def _check_length(value: str, min_len: int, max_len: int, *, empty: str, short: str) -> str:
    if not value:
        raise InvalidInputError(empty)
    if len(value) < min_len:
        raise InvalidInputError(short.format(limit=texts.chars(min_len)))
    if len(value) > max_len:
        # Не обрезаем молча: человек должен знать, что часть текста потерялась бы.
        raise InvalidInputError(
            texts.TOO_LONG.format(length=texts.chars(len(value)), limit=texts.chars(max_len))
        )
    return value


def validate_name(raw: str, limits: Limits) -> str:
    return _check_length(
        sanitize(raw),
        limits.name_min,
        limits.name_max,
        empty=texts.NAME_EMPTY,
        short=texts.NAME_SHORT,
    )


def validate_contact(raw: str, limits: Limits) -> str:
    """Почта, @username или ссылка t.me. Ник и ссылку приводим к виду @username."""
    value = sanitize(raw)
    _check_length(value, 1, limits.contact_max, empty=texts.CONTACT_EMPTY, short="")
    if _EMAIL_RE.fullmatch(value):
        return value
    if match := _TG_USERNAME_RE.fullmatch(value) or _TG_LINK_RE.fullmatch(value):
        return "@" + match.group(1)
    raise InvalidInputError(texts.CONTACT_INVALID)


def validate_task(raw: str, limits: Limits) -> str:
    return _check_length(
        sanitize(raw, multiline=True),
        limits.task_min,
        limits.task_max,
        empty=texts.TASK_EMPTY,
        short=texts.TASK_SHORT,
    )


def telegram_username_url(contact: str) -> str | None:
    """https://t.me/<ник> для контакта вида @username, иначе None."""
    if match := _TG_USERNAME_RE.fullmatch(contact):
        return f"https://t.me/{match.group(1)}"
    return None
