"""Оформление клиентских сообщений (этап 2в): разметка, экранирование, раскладка кнопок."""

from __future__ import annotations

import re

import pytest
from aiogram.types import InlineKeyboardMarkup, User

from bot import keyboards, texts
from bot.models import ApplicationDraft, ProjectType
from bot.services.formatting import format_consent, format_send_failed, format_summary
from tests.conftest import CLIENT_ID, FakeUser, MockSession, as_telegram_sees, callback_of
from tests.test_flow import APPLY

EVIL_NAME = "<b>Анна</b> & \"Ко\" 'ООО'"
EVIL_TASK = "Задача </blockquote><b>жирно</b> & <script>alert(1)</script>\nвторая строка"


def test_summary_escapes_input_and_quotes_task() -> None:
    draft = ApplicationDraft(
        name=EVIL_NAME,
        contact="a&b@mail.ru",
        project_type=ProjectType.SHOP,
        task=EVIL_TASK,
        consent=True,
    )
    html = format_summary(draft)
    assert "&lt;b&gt;Анна&lt;/b&gt; &amp; &quot;Ко&quot; &#x27;ООО&#x27;" in html
    assert "&lt;/blockquote&gt;" in html and "<script>" not in html
    plain, entities = as_telegram_sees(html)  # разметка сбалансирована
    assert EVIL_NAME in plain and EVIL_TASK in plain  # клиент видит ровно то, что ввёл
    (quote,) = [e for e in entities if e.type == "blockquote"]
    utf16 = plain.encode("utf-16-le")
    quoted = utf16[quote.offset * 2 : (quote.offset + quote.length) * 2].decode("utf-16-le")
    assert quoted == EVIL_TASK  # цитатой выделена именно задача, целиком
    assert plain.rstrip().endswith("Всё верно?")
    for label in ("Имя:", "Контакт:", "Проект:", "Задача:"):
        assert label in plain


@pytest.mark.parametrize(
    ("text", "step"),
    [
        (texts.ASK_NAME, 1),
        (texts.ASK_CONTACT, 2),
        (texts.ASK_PROJECT_TYPE, 3),
        (texts.ASK_TASK.format(limit=texts.chars(1000)), 4),
        (texts.ASK_CONSENT_NO_POLICY, 5),
        (format_consent("https://example.com/privacy?a=1&b=2"), 5),
    ],
)
def test_steps_have_progress_indicator(text: str, step: int) -> None:
    first_line = text.split("\n")[0]
    assert first_line == f"<b>Шаг {step} из 5</b> " + "●" * step + "○" * (5 - step)
    as_telegram_sees(text)


def test_all_client_texts_have_balanced_markup() -> None:
    filled = [
        texts.GREETING,
        texts.HELP,
        texts.CONTACT_INVALID,
        texts.TOO_LONG.format(length=texts.chars(81), limit=texts.chars(80)),
        texts.SENT.format(app_id="261020-676A"),
        format_send_failed("mail@example.com & <b>x</b>"),
        format_send_failed(None),
        texts.RATE_LIMITED.format(minutes=5),
        texts.CANCELLED,
        texts.CHOOSE_FIELD,
        texts.FALLBACK,
        texts.UNKNOWN_COMMAND,
        texts.UNEXPECTED_ERROR,
        texts.PROJECT_TYPE_CHOSEN.format(project_type="Лендинг"),
    ]
    for text in filled:
        as_telegram_sees(text)
    assert "&lt;b&gt;x&lt;/b&gt;" in format_send_failed("<b>x</b>")


def test_sent_shows_number_in_monospace() -> None:
    plain, entities = as_telegram_sees(texts.SENT.format(app_id="261020-676A"))
    assert plain == "✅ Заявка принята\nНомер: 261020-676A\nОтвечу в течение дня."
    assert [e.type for e in entities] == ["bold", "code"]


def test_popups_have_no_html() -> None:
    """callback.answer не разбирает HTML — теги показались бы клиенту как есть."""
    for popup in (texts.TG_NAME_UNUSABLE, texts.NO_USERNAME, texts.STALE_BUTTON):
        assert not re.search(r"</?\w+>", popup)


def test_greeting_keeps_only_site_facts() -> None:
    plain, _ = as_telegram_sees(texts.GREETING)
    for fact in ("Эдуарда Торгашова (Ferz)", "Челябинска", "удалённо", "в течение дня"):
        assert fact in plain
    assert not re.search(r"\d", plain)  # ни цен, ни сроков в цифрах


# ---------- раскладка клиентских клавиатур ----------
# Урок живой проверки 2б: три кнопки в ряд Telegram обрезал. Правило для клиента:
# не больше двух кнопок в ряду, и в ряду из двух — короткие подписи.

CLIENT_KEYBOARDS: list[tuple[str, InlineKeyboardMarkup]] = [
    ("apply", keyboards.apply_keyboard()),
    ("cancel", keyboards.cancel_keyboard()),
    ("name+tg", keyboards.name_keyboard(offer_tg_name=True)),
    ("contact", keyboards.contact_keyboard()),
    ("project_type", keyboards.project_type_keyboard()),
    ("consent", keyboards.consent_keyboard()),
    ("confirm", keyboards.confirm_keyboard()),
    ("edit_field", keyboards.edit_field_keyboard()),
]
SHORT_LABEL = 16  # «Интернет-магазин» — самая длинная подпись в паре, помещается


@pytest.mark.parametrize(("name", "markup"), CLIENT_KEYBOARDS)
def test_client_keyboards_do_not_truncate(name: str, markup: InlineKeyboardMarkup) -> None:
    for row in markup.inline_keyboard:
        assert len(row) <= 2, name
        if len(row) == 2:
            assert all(len(b.text) <= SHORT_LABEL for b in row), (name, [b.text for b in row])


async def test_dialog_shows_new_formatting(dp, bot, session: MockSession) -> None:  # type: ignore[no-untyped-def]
    user = FakeUser(dp, bot, session, User(id=CLIENT_ID, is_bot=False, first_name="Анна"))
    await user.send("/start")
    assert session.last_sent(CLIENT_ID).text == texts.GREETING
    await user.press(APPLY)
    await user.send(EVIL_NAME)
    await user.send("anna@mail.ru")
    await user.press(callback_of(session.last_sent(CLIENT_ID), "Лендинг"))
    await user.send(EVIL_TASK)
    await user.press(callback_of(session.last_sent(CLIENT_ID), texts.BTN_AGREE))
    summary = session.last_sent(CLIENT_ID).text
    plain, entities = as_telegram_sees(summary)
    assert "blockquote" in [e.type for e in entities]
    assert EVIL_NAME in plain and "<b>Анна</b>" not in summary
