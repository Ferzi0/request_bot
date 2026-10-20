"""Тестовая инфраструктура: бот без сети.

MockSession подменяет HTTP-сессию aiogram: каждый вызов Bot API записывается
в список, а в ответ возвращается правдоподобный объект. Апдейты подаются в
dp.feed_update — тот же путь, что проходит апдейт из long polling.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)

from bot.app import create_bot, create_dispatcher
from bot.config import Settings, load_settings

ADMIN_ID = 1000
CLIENT_ID = 2000
BOT_ID = 123456
FAKE_TOKEN = f"{BOT_ID}:TEST-token_ABCdefGHIjklMNOpqrSTUvwx"

Hook = Callable[[TelegramMethod[Any]], Any]

# Начало ответа клиенту «заявка принята» (texts.SENT) и номер из него.
ACCEPTED = "✅ <b>Заявка принята</b>"
_NUMBER_RE = re.compile(r"<code>(\d{6}-[0-9A-F]{4})</code>")


def app_number(text: str) -> str:
    """Номер заявки из ответа клиенту (он внутри <code>…</code>)."""
    match = _NUMBER_RE.search(text)
    assert match, f"нет номера заявки в {text!r}"
    return match.group(1)


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(1)
        # hook(method) может бросить исключение (имитация сбоя) или вернуть
        # значение; None — значит «отвечай по умолчанию».
        self.hook: Hook | None = None
        # Задержка ответа на запросы к этому чату — чтобы апдейты «пересеклись» во времени.
        self.slow_chat: int | None = None

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 — сигнатура задана aiogram
    ) -> Any:
        self.requests.append(method)
        if self.slow_chat is not None and getattr(method, "chat_id", None) == self.slow_chat:
            await asyncio.sleep(0.05)
        if self.hook is not None:
            result = self.hook(method)
            if result is not None:
                return result
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._ids),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"),
                text=method.text,
                reply_markup=method.reply_markup,
            )
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="ferz_test_bot")
        return True

    # --- удобные выборки для проверок ---
    def sent(self, chat_id: int | None = None) -> list[SendMessage]:
        return [
            m
            for m in self.requests
            if isinstance(m, SendMessage) and (chat_id is None or int(m.chat_id) == chat_id)
        ]

    def last_sent(self, chat_id: int) -> SendMessage:
        return self.sent(chat_id)[-1]

    def edits(self) -> list[EditMessageText]:
        return [m for m in self.requests if isinstance(m, EditMessageText)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return [m for m in self.requests if isinstance(m, AnswerCallbackQuery)]

    def markup_edits(self) -> list[EditMessageReplyMarkup]:
        return [m for m in self.requests if isinstance(m, EditMessageReplyMarkup)]

    def clear(self) -> None:
        self.requests.clear()


class FakeUser:
    """Собеседник бота: шлёт текст, нестандартные сообщения и жмёт кнопки."""

    _update_ids = itertools.count(1)

    def __init__(
        self,
        dp: Dispatcher,
        bot: Bot,
        session: MockSession,
        user: User,
        chat_type: str = "private",
        chat_id: int | None = None,
    ) -> None:
        self.dp, self.bot, self.session, self.user = dp, bot, session, user
        self.chat = Chat(id=chat_id or user.id, type=chat_type, title=None)
        self._msg_ids = itertools.count(10_000)

    def _message(self, **fields: Any) -> Message:
        return Message(
            message_id=next(self._msg_ids),
            date=datetime.now(UTC),
            chat=self.chat,
            from_user=self.user,
            **fields,
        )

    async def feed(self, update: Update) -> None:
        await self.dp.feed_update(self.bot, update)

    async def send(self, text: str) -> None:
        await self.feed(Update(update_id=next(self._update_ids), message=self._message(text=text)))

    async def send_sticker_like(self) -> None:
        # Сообщение без текста (как стикер/фото): для хендлеров важно лишь, что text=None.
        msg = self._message(caption="фото")
        await self.feed(Update(update_id=next(self._update_ids), message=msg))

    async def press(
        self, callback_data: str, *, on: SendMessage | None = None, message: Message | None = None
    ) -> None:
        """Нажать кнопку под сообщением бота (по умолчанию — под последним).

        message — готовое сообщение в том виде, в каком его пришлёт Telegram
        (например, с entities вместо HTML-тегов).
        """
        history = self.session.sent(self.chat.id)
        source = on or (history[-1] if history else None)
        bot_message = message or Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=self.chat,
            from_user=User(id=BOT_ID, is_bot=True, first_name="Bot"),
            text=source.text if source else "…",
            reply_markup=source.reply_markup if source else None,
        )
        callback = CallbackQuery(
            id=str(next(self._update_ids)),
            from_user=self.user,
            chat_instance="ci",
            message=bot_message,
            data=callback_data,
        )
        await self.feed(Update(update_id=next(self._update_ids), callback_query=callback))


class _TelegramHtml(HTMLParser):
    """Разбор нашего HTML так, как это делает Telegram: текст + entities (UTF-16)."""

    _TYPES: ClassVar[dict[str, str]] = {
        "b": "bold",
        "code": "code",
        "a": "text_link",
        "i": "italic",
        "blockquote": "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.entities: list[MessageEntity] = []
        self._open: list[tuple[str, int, str | None]] = []

    def _pos(self) -> int:
        return len(self.text.encode("utf-16-le")) // 2

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open.append((tag, self._pos(), dict(attrs).get("href")))

    def handle_endtag(self, tag: str) -> None:
        name, start, href = self._open.pop()
        assert name == tag, f"разметка не сбалансирована: <{name}> … </{tag}>"
        self.entities.append(
            MessageEntity(type=self._TYPES[tag], offset=start, length=self._pos() - start, url=href)
        )

    def handle_data(self, data: str) -> None:
        self.text += data


def as_telegram_sees(html_text: str) -> tuple[str, list[MessageEntity]]:
    """Текст и entities, которые Telegram сохранит у сообщения с этим HTML."""
    parser = _TelegramHtml()
    parser.feed(html_text)
    parser.close()
    assert not parser._open, "незакрытые теги"
    return parser.text, parser.entities


def buttons(method: SendMessage) -> list[tuple[str, str | None, str | None]]:
    """[(текст, callback_data, url)] всех кнопок сообщения."""
    markup = method.reply_markup
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [(b.text, b.callback_data, b.url) for row in markup.inline_keyboard for b in row]


def callback_of(method: SendMessage, text: str) -> str:
    for btn_text, data, _ in buttons(method):
        if btn_text == text and data:
            return data
    raise AssertionError(f"Нет кнопки {text!r}: {buttons(method)}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Переменные окружения разработчика не должны влиять на тесты."""
    import os

    for name in list(os.environ):
        if name in Settings.model_fields or name.lower() in Settings.model_fields:
            monkeypatch.delenv(name)
    # Страховка: даже тест, запускающий bot.__main__.run(), не создаст data/bot.db
    # в проекте — база по умолчанию уходит во временную папку теста.
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "default.db"))


@pytest.fixture
def settings() -> Settings:
    return load_settings(
        _env_file=None,  # не читать настоящий .env разработчика
        bot_token=FAKE_TOKEN,
        admin_id=ADMIN_ID,
        fallback_contact="mail@example.com или @example",
        send_retry_delays=(0.01, 0.01),
        notify_admin_on_start=False,
    )


@pytest.fixture
def session() -> MockSession:
    return MockSession()


@pytest.fixture
async def bot(settings: Settings, session: MockSession) -> AsyncIterator[Bot]:
    bot = create_bot(settings, session=session)
    yield bot
    await bot.session.close()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def dp(settings: Settings, sleeps: list[float]) -> Dispatcher:
    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    return create_dispatcher(settings, sleep=fake_sleep)


@pytest.fixture
def client(dp: Dispatcher, bot: Bot, session: MockSession) -> FakeUser:
    user = User(id=CLIENT_ID, is_bot=False, first_name="Анна", last_name="К", username="anna_k")
    return FakeUser(dp, bot, session, user)


@pytest.fixture
def admin(dp: Dispatcher, bot: Bot, session: MockSession) -> FakeUser:
    user = User(id=ADMIN_ID, is_bot=False, first_name="Эдуард", username="ferz_admin")
    return FakeUser(dp, bot, session, user)
