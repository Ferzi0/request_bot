"""Сквозные сценарии диалога через dp.feed_update (без сети)."""

from __future__ import annotations

from aiogram.types import User

from bot import texts
from bot.callbacks import EditField, EditFieldCallback, FormAction, FormCallback
from tests.conftest import (
    ADMIN_ID,
    CLIENT_ID,
    FakeUser,
    MockSession,
    app_number,
    buttons,
    callback_of,
)

APPLY = FormCallback(action=FormAction.APPLY).pack()
CANCEL = FormCallback(action=FormAction.CANCEL).pack()
SUBMIT = FormCallback(action=FormAction.SUBMIT).pack()

TASK = "Нужен лендинг для автошколы с формой записи и ценами."


async def fill_until_summary(
    client: FakeUser, session: MockSession, *, start: str = "/start"
) -> None:
    await client.send(start)
    await client.press(APPLY)
    await client.send("Анна")
    await client.send("anna@mail.ru")
    await client.press(callback_of(session.last_sent(client.chat.id), "Лендинг"))
    await client.send(TASK)
    await client.press(callback_of(session.last_sent(client.chat.id), texts.BTN_AGREE))


async def test_happy_path_from_site(client: FakeUser, session: MockSession) -> None:
    await client.send("/start site")
    greeting = session.last_sent(CLIENT_ID)
    assert greeting.text == texts.GREETING
    assert [b[0] for b in buttons(greeting)] == [texts.BTN_APPLY]

    await client.press(APPLY)
    assert session.last_sent(CLIENT_ID).text == texts.ASK_NAME

    await client.send("  Анна ​ ")
    assert session.last_sent(CLIENT_ID).text == texts.ASK_CONTACT

    await client.send("anna@mail.ru")
    assert session.last_sent(CLIENT_ID).text == texts.ASK_PROJECT_TYPE
    labels = [b[0] for b in buttons(session.last_sent(CLIENT_ID))]
    assert labels == [
        "Сайт-визитка",
        "Лендинг",
        "Telegram-бот",
        "Веб-сервис",
        "Интернет-магазин",
        "Другое",
        texts.BTN_CANCEL,
    ]

    await client.press(callback_of(session.last_sent(CLIENT_ID), "Лендинг"))
    assert session.edits()[-1].text == texts.PROJECT_TYPE_CHOSEN.format(project_type="Лендинг")
    assert session.last_sent(CLIENT_ID).text.startswith("<b>Шаг 4 из 5</b>")

    await client.send(TASK)
    consent = session.last_sent(CLIENT_ID)
    assert consent.text == texts.ASK_CONSENT_NO_POLICY  # POLICY_URL не задан

    await client.press(callback_of(consent, texts.BTN_AGREE))
    summary = session.last_sent(CLIENT_ID)
    assert "Проверьте заявку" in summary.text
    assert "Анна" in summary.text and "anna@mail.ru" in summary.text and "Лендинг" in summary.text

    session.clear()
    await client.press(SUBMIT)

    to_admin = session.sent(ADMIN_ID)
    assert len(to_admin) == 1
    admin_text = to_admin[0].text
    assert "Новая заявка №" in admin_text
    assert "источник: сайт" in admin_text
    assert "<b>Имя:</b> Анна\n" in admin_text  # невидимый символ и пробелы вычищены
    assert f"ID: <code>{CLIENT_ID}</code>" in admin_text
    assert "Ник: @anna_k" in admin_text
    assert TASK in admin_text
    admin_buttons = buttons(to_admin[0])
    assert (texts.BTN_WRITE_TO_CLIENT, None, "https://t.me/anna_k") in admin_buttons
    assert any(b[0] == texts.BTN_TAKE and b[1] for b in admin_buttons)

    done = session.last_sent(CLIENT_ID).text
    assert done == texts.SENT.format(app_id=app_number(done))


async def test_source_is_bot_without_deep_link(client: FakeUser, session: MockSession) -> None:
    await fill_until_summary(client, session, start="/start")
    await client.press(SUBMIT)
    assert "источник: бот" in session.sent(ADMIN_ID)[-1].text


async def test_unknown_deep_link_is_bot(client: FakeUser, session: MockSession) -> None:
    await fill_until_summary(client, session, start="/start <b>x</b>")
    await client.press(SUBMIT)
    assert "источник: бот" in session.sent(ADMIN_ID)[-1].text


async def test_tg_name_and_username_buttons(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")
    await client.press(APPLY)
    await client.press(callback_of(session.last_sent(CLIENT_ID), texts.BTN_USE_TG_NAME))
    assert session.last_sent(CLIENT_ID).text == texts.ASK_CONTACT
    await client.press(callback_of(session.last_sent(CLIENT_ID), texts.BTN_USE_TG_USERNAME))
    assert session.last_sent(CLIENT_ID).text == texts.ASK_PROJECT_TYPE
    await client.press(callback_of(session.last_sent(CLIENT_ID), "Другое"))
    await client.send(TASK)
    await client.press(callback_of(session.last_sent(CLIENT_ID), texts.BTN_AGREE))
    summary = session.last_sent(CLIENT_ID).text
    assert "<b>Имя:</b> Анна" in summary
    assert "<b>Контакт:</b> @anna_k" in summary


async def test_username_button_without_username(dp, bot, session: MockSession) -> None:
    user = User(id=3000, is_bot=False, first_name="Ли")  # нет ника, имя короче 2 символов
    nouser = FakeUser(dp, bot, session, user)
    await nouser.send("/start")
    await nouser.press(APPLY)
    # Имя «Ли» из 2 букв проходит; кнопку имени показываем
    assert any(b[0] == texts.BTN_USE_TG_NAME for b in buttons(session.last_sent(3000)))
    await nouser.send("Ли Вэй")
    session.clear()
    await nouser.press(FormCallback(action=FormAction.USE_TG_USERNAME).pack())
    alert = session.answers()[-1]
    assert alert.text == texts.NO_USERNAME and alert.show_alert
    assert session.sent(3000) == []  # остались на шаге контакта
    await nouser.send("@li_wei_dev")
    assert session.last_sent(3000).text == texts.ASK_PROJECT_TYPE


async def test_invalid_inputs(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")
    await client.press(APPLY)

    await client.send("А")
    assert session.last_sent(CLIENT_ID).text == "Слишком коротко — нужно не меньше 2 символа."
    await client.send("Я" * 81)
    assert "81 символ при лимите 80 символов" in session.last_sent(CLIENT_ID).text
    await client.send_sticker_like()
    assert session.last_sent(CLIENT_ID).text == texts.TEXT_ONLY
    await client.send("Анна")

    await client.send("не почта")
    assert session.last_sent(CLIENT_ID).text == texts.CONTACT_INVALID
    await client.send("https://t.me/anna_k")
    assert session.last_sent(CLIENT_ID).text == texts.ASK_PROJECT_TYPE

    await client.send("Лендинг")  # текстом, а не кнопкой
    assert session.last_sent(CLIENT_ID).text == texts.USE_BUTTONS
    await client.press(callback_of(session.sent(CLIENT_ID)[-2], "Лендинг"))

    await client.send("коротко")
    task_short = "Чуть подробнее, пожалуйста — не меньше 10 символов."
    assert session.last_sent(CLIENT_ID).text == task_short
    await client.send("x" * 1001)
    assert "1001 символ при лимите 1000 символов" in session.last_sent(CLIENT_ID).text
    await client.send_sticker_like()
    assert session.last_sent(CLIENT_ID).text == texts.TEXT_ONLY
    await client.send(TASK)
    assert session.last_sent(CLIENT_ID).text == texts.ASK_CONSENT_NO_POLICY
    await client.press(callback_of(session.last_sent(CLIENT_ID), texts.BTN_AGREE))
    summary_contact = session.last_sent(CLIENT_ID).text
    assert "<b>Контакт:</b> @anna_k" in summary_contact  # ссылка t.me приведена к @нику


async def test_cancel_by_command_and_button(client: FakeUser, session: MockSession) -> None:
    await client.send("/cancel")
    assert session.last_sent(CLIENT_ID).text == texts.NOTHING_TO_CANCEL

    await client.send("/start")
    await client.press(APPLY)
    await client.send("Анна")
    await client.send("/cancel")
    assert session.last_sent(CLIENT_ID).text == texts.CANCELLED
    # после отмены текст уже не принимается как ответ на шаг
    await client.send("anna@mail.ru")
    assert session.last_sent(CLIENT_ID).text == texts.FALLBACK

    await client.press(APPLY)
    await client.send("Анна")
    await client.press(CANCEL)
    assert session.last_sent(CLIENT_ID).text == texts.CANCELLED
    assert session.sent(ADMIN_ID) == []


async def test_cancel_on_summary(client: FakeUser, session: MockSession) -> None:
    await fill_until_summary(client, session)
    await client.press(CANCEL)
    assert session.last_sent(CLIENT_ID).text == texts.CANCELLED
    session.clear()
    await client.press(SUBMIT)  # старая кнопка «Отправить»
    assert session.sent(ADMIN_ID) == []
    assert session.answers()[-1].text == texts.STALE_BUTTON


async def test_edit_field_returns_to_summary(client: FakeUser, session: MockSession) -> None:
    await fill_until_summary(client, session)
    await client.press(FormCallback(action=FormAction.EDIT).pack())
    assert session.last_sent(CLIENT_ID).text == texts.CHOOSE_FIELD
    await client.press(EditFieldCallback(field=EditField.CONTACT).pack())
    assert session.last_sent(CLIENT_ID).text == texts.ASK_CONTACT
    await client.send("new@mail.ru")
    summary = session.last_sent(CLIENT_ID).text
    assert "Проверьте заявку" in summary and "new@mail.ru" in summary

    await client.press(FormCallback(action=FormAction.EDIT).pack())
    await client.press(EditFieldCallback(field=EditField.PROJECT_TYPE).pack())
    await client.press(callback_of(session.last_sent(CLIENT_ID), "Веб-сервис"))
    summary = session.last_sent(CLIENT_ID).text
    assert "Веб-сервис" in summary and "Проверьте заявку" in summary

    await client.press(FormCallback(action=FormAction.EDIT).pack())
    await client.press(FormCallback(action=FormAction.BACK_TO_SUMMARY).pack())
    assert "Проверьте заявку" in session.last_sent(CLIENT_ID).text

    await client.press(SUBMIT)
    assert "new@mail.ru" in session.sent(ADMIN_ID)[-1].text


async def test_stale_buttons_are_ignored(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")
    await client.press(APPLY)
    name_question = session.last_sent(CLIENT_ID)
    await client.send("Анна")
    session.clear()
    # «Использовать имя из Telegram» с прошлого шага не должно менять ответ
    await client.press(callback_of(name_question, texts.BTN_USE_TG_NAME), on=name_question)
    assert session.answers()[-1].text == texts.STALE_BUTTON
    assert session.sent(CLIENT_ID) == []


async def test_policy_url_in_consent(settings, bot, session: MockSession) -> None:
    from bot.app import create_dispatcher

    custom = settings.model_copy(
        update={"policy_url": 'https://ferz.example/privacy.html?a=1&b="2"'}
    )
    dp = create_dispatcher(custom)
    user = FakeUser(dp, bot, session, User(id=4000, is_bot=False, first_name="Олег"))
    await user.send("/start")
    await user.press(APPLY)
    await user.send("Олег")
    await user.send("oleg@mail.ru")
    await user.press(callback_of(session.last_sent(4000), "Сайт-визитка"))
    await user.send(TASK)
    text = session.last_sent(4000).text
    assert '<a href="https://ferz.example/privacy.html?a=1&amp;b=&quot;2&quot;">' in text


async def test_help_and_fallback(client: FakeUser, session: MockSession) -> None:
    await client.send("/help")
    assert session.last_sent(CLIENT_ID).text == texts.HELP
    await client.send("привет")
    assert session.last_sent(CLIENT_ID).text == texts.FALLBACK


async def test_commands_are_not_saved_as_answers(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")
    await client.press(APPLY)
    await client.send("/foo")  # шаг имени
    assert session.last_sent(CLIENT_ID).text == texts.UNKNOWN_COMMAND
    await client.send("/settings@ferz_test_bot")
    assert session.last_sent(CLIENT_ID).text == texts.UNKNOWN_COMMAND
    await client.send("/ Анна")  # «/» с пробелом — не команда, а (странный) ответ
    assert session.last_sent(CLIENT_ID).text == texts.ASK_CONTACT
    await client.send("/bar")  # шаг контакта
    assert session.last_sent(CLIENT_ID).text == texts.UNKNOWN_COMMAND
    await client.send("anna@mail.ru")
    await client.press(callback_of(session.last_sent(CLIENT_ID), "Лендинг"))
    await client.send("/task_text")  # шаг задачи
    assert session.last_sent(CLIENT_ID).text == texts.UNKNOWN_COMMAND
    await client.send(TASK)
    await client.press(callback_of(session.last_sent(CLIENT_ID), texts.BTN_AGREE))
    summary = session.last_sent(CLIENT_ID).text
    assert "/foo" not in summary and "/task_text" not in summary
    assert "<b>Имя:</b> / Анна" in summary


def test_russian_plural_of_chars() -> None:
    cases = {
        1: "1 символ",
        2: "2 символа",
        4: "4 символа",
        5: "5 символов",
        11: "11 символов",
        12: "12 символов",
        21: "21 символ",
        22: "22 символа",
        80: "80 символов",
        81: "81 символ",
        111: "111 символов",
        1001: "1001 символ",
    }
    for count, expected in cases.items():
        assert texts.chars(count) == expected
