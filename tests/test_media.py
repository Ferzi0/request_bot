"""Картинки клиенту (приветствие, «Заявка принята») и стили кнопок — этап 2д."""

from __future__ import annotations

import asyncio
import logging
import struct
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import (
    EditMessageReplyMarkup,
    EditMessageText,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import FSInputFile, InlineKeyboardMarkup, User

from bot import keyboards, texts
from bot.app import create_dispatcher
from bot.config import Settings
from bot.keyboards import ButtonStyle
from bot.models import Applicant, Application, ApplicationRecord, ApplicationStatus, ProjectType
from bot.services.media import ASSETS_DIR, CAPTION_LIMIT, PhotoSender, Picture, fits_caption
from tests.conftest import (
    ACCEPTED,
    ADMIN_ID,
    CLIENT_ID,
    FakeUser,
    MockSession,
    app_number,
    as_telegram_sees,
)
from tests.test_flow import APPLY, SUBMIT, TASK, fill_until_summary

PERSONAL = ("Анна", "anna@mail.ru", "anna_k", TASK)


def photo_name(method: SendPhoto) -> str:
    """Имя файла, если фото загружается, или file_id, если отправлено из кэша."""
    photo = method.photo
    return Path(photo.path).name if isinstance(photo, FSInputFile) else str(photo)


def fail_photos(exc_factory):  # type: ignore[no-untyped-def]
    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, SendPhoto):
            raise exc_factory(method)
        return None

    return hook


# ---------- приветствие ----------


async def test_greeting_is_photo_with_caption_and_button(
    client: FakeUser, session: MockSession
) -> None:
    await client.send("/start")
    (photo,) = session.photos(CLIENT_ID)
    assert photo.caption == texts.GREETING
    assert photo_name(photo) == "welcome.jpg"
    assert isinstance(photo.reply_markup, InlineKeyboardMarkup)
    (button,) = [b for row in photo.reply_markup.inline_keyboard for b in row]
    assert (button.text, button.style) == (texts.BTN_APPLY, "success")
    # Обычным текстом приветствие не дублируется
    assert not [m for m in session.requests if isinstance(m, SendMessage)]


async def test_second_start_uses_cached_file_id(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")
    await client.send("/start")
    first, second = session.photos(CLIENT_ID)
    assert isinstance(first.photo, FSInputFile)  # загрузка файла — один раз
    assert second.photo == "file-id-welcome.jpg"  # дальше — по file_id


async def test_cache_is_dropped_when_file_id_fails(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")

    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, SendPhoto) and isinstance(method.photo, str):
            raise TelegramBadRequest(method, "Bad Request: wrong file identifier")
        return None

    session.hook = hook
    await client.send("/start")  # file_id отвергнут → текст
    assert session.last_sent(CLIENT_ID).text == texts.GREETING
    await client.send("/start")  # кэш сброшен → файл загружается заново
    assert isinstance(session.photos(CLIENT_ID)[-1].photo, FSInputFile)


@pytest.mark.parametrize(
    "error",
    [
        lambda m: TelegramBadRequest(m, "Bad Request: IMAGE_PROCESS_FAILED"),
        lambda m: TelegramNetworkError(m, "timeout"),
    ],
)
async def test_greeting_falls_back_to_text(
    client: FakeUser,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
    error,  # type: ignore[no-untyped-def]
) -> None:
    session.hook = fail_photos(error)
    await client.send("/start")
    greeting = session.last_sent(CLIENT_ID)
    assert not session.is_photo(greeting)
    assert greeting.text == texts.GREETING
    assert greeting.reply_markup == keyboards.apply_keyboard()  # та же кнопка
    assert "Картинка welcome.jpg не отправлена" in caplog.text
    await client.press(APPLY)  # диалог продолжается
    assert session.last_sent(CLIENT_ID).text == texts.ASK_NAME


async def test_missing_file_falls_back_to_text(
    settings: Settings, bot: Bot, session: MockSession, tmp_path: Path
) -> None:
    dp = create_dispatcher(settings)
    dp["media"] = PhotoSender(assets_dir=tmp_path)  # пустая папка — картинок нет
    client = FakeUser(dp, bot, session, User(id=CLIENT_ID, is_bot=False, first_name="Анна"))
    await client.send("/start")
    assert session.photos() == []
    assert session.last_sent(CLIENT_ID).text == texts.GREETING


async def test_apply_on_photo_greeting_does_not_edit_text(
    client: FakeUser, session: MockSession
) -> None:
    await client.send("/start")
    assert session.is_photo(session.last_sent(CLIENT_ID))
    session.clear()
    await client.press(APPLY)  # кнопка под фото: в callback сообщение с photo, без text
    assert not [m for m in session.requests if isinstance(m, EditMessageText)]
    assert [m for m in session.requests if isinstance(m, EditMessageReplyMarkup)]
    assert session.last_sent(CLIENT_ID).text == texts.ASK_NAME
    assert texts.UNEXPECTED_ERROR not in [m.text for m in session.sent(CLIENT_ID)]


async def test_stale_apply_button_on_photo(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")
    greeting = session.last_sent(CLIENT_ID)
    await client.press(APPLY, on=greeting)
    await client.send("Анна")
    session.clear()
    await client.press(APPLY, on=greeting)  # снова старая кнопка — новая заявка с начала
    assert texts.UNEXPECTED_ERROR not in [m.text for m in session.sent(CLIENT_ID)]
    assert not [m for m in session.requests if isinstance(m, EditMessageText)]


# ---------- «Заявка принята» ----------


async def test_accepted_is_photo_after_admin_delivery(
    client: FakeUser, session: MockSession
) -> None:
    await fill_until_summary(client, session)
    session.clear()
    await client.press(SUBMIT)
    kinds = [
        (type(m).__name__, int(getattr(m, "chat_id", 0) or 0))
        for m in session.requests
        if isinstance(m, SendMessage | SendPhoto)
    ]
    assert kinds == [("SendMessage", ADMIN_ID), ("SendPhoto", CLIENT_ID)]
    (photo,) = session.photos(CLIENT_ID)
    assert photo_name(photo) == "accepted.jpg"
    assert photo.caption == texts.SENT.format(app_id=app_number(photo.caption or ""))
    assert photo.reply_markup is None


@pytest.mark.parametrize(
    "error",
    [
        lambda m: TelegramBadRequest(m, "Bad Request: Анна anna@mail.ru"),
        lambda m: TelegramNetworkError(m, "timeout"),
    ],
)
async def test_accepted_photo_failure_sends_text_once(
    client: FakeUser,
    session: MockSession,
    caplog: pytest.LogCaptureFixture,
    error,  # type: ignore[no-untyped-def]
) -> None:
    await fill_until_summary(client, session)
    session.hook = fail_photos(error)
    caplog.set_level(logging.INFO)
    await client.press(SUBMIT)
    confirmation = session.last_sent(CLIENT_ID)
    assert confirmation.text.startswith(ACCEPTED) and not session.is_photo(confirmation)
    assert len(session.sent(ADMIN_ID)) == 1  # заявка администратору — ровно одна
    texts_to_client = [m.text for m in session.sent(CLIENT_ID)]
    assert not any(t.startswith("⚠️ <b>Не получилось отправить заявку") for t in texts_to_client)
    for personal in PERSONAL:
        assert personal not in caplog.text


async def test_admin_never_gets_photos(
    client: FakeUser, admin: FakeUser, session: MockSession
) -> None:
    """Уведомление о заявке, «В работу» и панель — без картинок (решение заказчика)."""
    await fill_until_summary(client, session)
    await client.press(SUBMIT)
    notification = session.last_sent(ADMIN_ID)
    await admin.press(
        [b.callback_data for row in notification.reply_markup.inline_keyboard for b in row][-1]
        or "",
        on=notification,
    )
    await admin.send("/admin")
    assert session.photos(ADMIN_ID) == []
    # Клиенту — только приветствие (/start) и «Заявка принята»
    assert [photo_name(p) for p in session.photos(CLIENT_ID)] == [
        "welcome.jpg",
        "accepted.jpg",
    ]


# ---------- подписи ----------


def test_captions_fit_limit() -> None:
    for caption in (texts.GREETING, texts.SENT.format(app_id="261020-676A")):
        assert fits_caption(caption)
        plain, _ = as_telegram_sees(caption)
        assert len(plain.encode("utf-16-le")) // 2 <= CAPTION_LIMIT
    assert not fits_caption("😀" * 513)  # 1026 UTF-16


async def test_too_long_caption_goes_as_text(bot: Bot, session: MockSession) -> None:
    long = "Длинный текст. " * 80  # > 1024
    sent = await PhotoSender().send(bot, CLIENT_ID, Picture.WELCOME, long)
    assert session.photos() == [] and sent.text == long


# ---------- файлы картинок ----------


def _jpeg_size(path: Path) -> tuple[int, int]:
    """Ширина и высота JPEG из маркера SOF — без Pillow."""
    data = path.read_bytes()
    assert data[:2] == b"\xff\xd8", "не JPEG"
    i = 2
    while i < len(data):
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    raise AssertionError("нет SOF")


@pytest.mark.parametrize("picture", list(Picture))
def test_picture_files(picture: Picture) -> None:
    path = ASSETS_DIR / picture.value
    assert path.is_file()
    assert path.stat().st_size <= 300 * 1024
    width, height = _jpeg_size(path)
    assert (width, height) == (1280, 720)  # 16:9


# ---------- стили кнопок ----------

APP = Application(
    id="261020-0001",
    created_at=datetime(2026, 10, 20, tzinfo=UTC),
    source="bot",
    name="Анна",
    contact="anna@mail.ru",
    project_type=ProjectType.LANDING,
    task=TASK,
    applicant=Applicant(user_id=CLIENT_ID, username="anna_k", full_name="Анна"),
)


def styles(markup: InlineKeyboardMarkup) -> dict[str, str | None]:
    return {b.text: b.style for row in markup.inline_keyboard for b in row}


def card(status: ApplicationStatus) -> InlineKeyboardMarkup:
    record = ApplicationRecord(application=APP, status=status)
    return keyboards.panel_card_keyboard(record, "https://t.me/anna_k", "all", 0)


EXPECTED_STYLES: list[tuple[str, InlineKeyboardMarkup, dict[str, str]]] = [
    ("приветствие", keyboards.apply_keyboard(), {texts.BTN_APPLY: "success"}),
    ("согласие", keyboards.consent_keyboard(), {texts.BTN_AGREE: "success"}),
    ("сводка", keyboards.confirm_keyboard(), {texts.BTN_SUBMIT: "success"}),
    ("отмена", keyboards.cancel_keyboard(), {}),
    ("имя", keyboards.name_keyboard(offer_tg_name=True), {}),
    ("контакт", keyboards.contact_keyboard(), {}),
    ("тип проекта", keyboards.project_type_keyboard(), {}),
    ("поле", keyboards.edit_field_keyboard(), {}),
    (
        "уведомление",
        keyboards.admin_application_keyboard("261020-0001", "https://t.me/anna_k"),
        {texts.BTN_TAKE: "primary"},
    ),
    ("панель", keyboards.panel_home_keyboard(dict.fromkeys(ApplicationStatus, 3)), {}),
    ("список", keyboards.panel_list_keyboard([], "all", 1, has_next=True), {}),
    (
        "карточка new",
        card(ApplicationStatus.NEW),
        {texts.PANEL_BTN_TAKE: "primary", texts.PANEL_BTN_CANCEL_ACTION: "danger"},
    ),
    (
        "карточка in_work",
        card(ApplicationStatus.IN_WORK),
        {texts.PANEL_BTN_DONE_ACTION: "success", texts.PANEL_BTN_CANCEL_ACTION: "danger"},
    ),
    ("карточка done", card(ApplicationStatus.DONE), {}),
    ("карточка cancelled", card(ApplicationStatus.CANCELLED), {}),
    (
        "подтверждение",
        keyboards.panel_confirm_cancel_keyboard("261020-0001", "all", 0),
        {texts.PANEL_BTN_CONFIRM_CANCEL: "danger"},
    ),
    ("в панель", keyboards.panel_home_only_keyboard(), {}),
]


@pytest.mark.parametrize(("name", "markup", "expected"), EXPECTED_STYLES)
def test_button_styles(name: str, markup: InlineKeyboardMarkup, expected: dict[str, str]) -> None:
    actual = styles(markup)
    assert set(expected) <= set(actual), name
    for label, style in actual.items():
        assert style == expected.get(label), (name, label)


def test_only_three_telegram_styles() -> None:
    assert {s.value for s in ButtonStyle} == {"primary", "success", "danger"}


def test_style_is_sent_to_telegram() -> None:
    button = keyboards.apply_keyboard().inline_keyboard[0][0]
    assert button.model_dump(exclude_none=True)["style"] == "success"


async def test_dp_fixture_media_is_default(dp: Dispatcher) -> None:
    assert isinstance(dp["media"], PhotoSender)


# ---------- антибаг 2д: сбои, кэш, путь к файлам ----------


@pytest.mark.parametrize(
    "error",
    [
        lambda m: TelegramRetryAfter(m, "Too Many Requests", retry_after=3),
        lambda m: TelegramServerError(m, "Internal Server Error"),
        lambda m: TelegramForbiddenError(m, "Forbidden: photos are not allowed"),
        lambda m: RuntimeError("что угодно"),
    ],
)
async def test_any_photo_error_falls_back_to_same_text(
    client: FakeUser,
    session: MockSession,
    error,  # type: ignore[no-untyped-def]
) -> None:
    session.hook = fail_photos(error)
    await client.send("/start")
    greeting = session.last_sent(CLIENT_ID)
    assert not session.is_photo(greeting)
    assert (greeting.text, greeting.reply_markup) == (texts.GREETING, keyboards.apply_keyboard())


async def test_blocked_client_at_submit(
    client: FakeUser, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    """Клиент заблокировал бота до «Заявка принята»: заявка у администратора одна,
    «Не получилось отправить» не пытаемся слать, ERROR в логе нет."""
    await fill_until_summary(client, session)

    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, SendMessage | SendPhoto) and int(method.chat_id) == CLIENT_ID:
            raise TelegramForbiddenError(method, "Forbidden: bot was blocked by the user")
        return None

    session.hook = hook
    session.clear()
    caplog.set_level(logging.INFO)
    await client.press(SUBMIT)
    assert len(session.sent(ADMIN_ID)) == 1
    to_client = [
        m.caption if isinstance(m, SendPhoto) else m.text
        for m in session.requests
        if isinstance(m, SendMessage | SendPhoto) and int(m.chat_id) == CLIENT_ID
    ]
    assert len(to_client) == 2 and all((t or "").startswith(ACCEPTED) for t in to_client)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "заблокировал бота" in caplog.text


async def test_largest_photo_size_is_cached_and_not_logged(
    client: FakeUser, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await client.send("/start")
    session.hook = fail_photos(lambda m: TelegramBadRequest(m, "Bad Request: wrong file id"))
    await client.send("/start")
    (_, second) = session.photos(CLIENT_ID)
    assert second.photo == "file-id-welcome.jpg"  # оригинал, а не уменьшенная копия
    assert "file-id" not in caplog.text  # file_id в лог не попадает


async def test_stale_file_id_does_not_drop_fresh_one(bot: Bot, session: MockSession) -> None:
    """Пока запрос со старым file_id шёл, другой клиент загрузил файл заново:
    сбой старого запроса не должен стереть свежий file_id."""
    sender = PhotoSender()
    await sender.send(bot, CLIENT_ID, Picture.WELCOME, texts.GREETING)

    def hook(method: TelegramMethod) -> None:  # type: ignore[type-arg]
        if isinstance(method, SendPhoto) and method.photo == "file-id-welcome.jpg":
            sender._file_ids[Picture.WELCOME] = "fresh-id"  # «параллельная» загрузка
            raise TelegramBadRequest(method, "Bad Request: wrong file identifier")
        return None

    session.hook = hook
    await sender.send(bot, CLIENT_ID, Picture.WELCOME, texts.GREETING)
    session.hook = None
    await sender.send(bot, CLIENT_ID, Picture.WELCOME, texts.GREETING)
    assert session.photos()[-1].photo == "fresh-id"


async def test_two_first_sends_at_once(bot: Bot, session: MockSession) -> None:
    """Два клиента одновременно после запуска: обе загрузки допустимы, кэш цел,
    и file_id годится для любого чата (он принадлежит боту, а не чату)."""
    sender = PhotoSender()
    session.slow_chat = CLIENT_ID
    await asyncio.gather(
        sender.send(bot, CLIENT_ID, Picture.WELCOME, texts.GREETING),
        sender.send(bot, ADMIN_ID + 1, Picture.WELCOME, texts.GREETING),
    )
    assert all(isinstance(p.photo, FSInputFile) for p in session.photos())
    await sender.send(bot, ADMIN_ID + 2, Picture.WELCOME, texts.GREETING)
    assert session.photos()[-1].photo == "file-id-welcome.jpg"


async def test_assets_do_not_depend_on_cwd(
    bot: Bot, session: MockSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Под launchd/systemd рабочая папка другая — картинки всё равно находятся."""
    assert ASSETS_DIR.is_absolute()
    monkeypatch.chdir(tmp_path)
    await PhotoSender().send(bot, CLIENT_ID, Picture.ACCEPTED, "x")
    (photo,) = session.photos()
    assert photo_name(photo) == "accepted.jpg"
