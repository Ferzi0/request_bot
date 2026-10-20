"""Пересылка администратору: экранирование, кнопки, сбои, повторы, доступ, антифлуд."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import EditMessageText, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import Chat, Message, User

from bot import texts
from bot.callbacks import FormAction, FormCallback
from tests.conftest import (
    ACCEPTED,
    ADMIN_ID,
    CLIENT_ID,
    FakeUser,
    MockSession,
    as_telegram_sees,
    buttons,
    callback_of,
)
from tests.test_flow import APPLY, SUBMIT, TASK, fill_until_summary


def fail_admin_with(exc_factory, times: int | None = None):
    """Hook: сообщения администратору падают `times` раз (None — всегда)."""
    calls = {"n": 0}

    def hook(method: TelegramMethod) -> None:
        if isinstance(method, SendMessage) and int(method.chat_id) == ADMIN_ID:
            calls["n"] += 1
            if times is None or calls["n"] <= times:
                raise exc_factory(method)
        return None

    return hook


async def test_user_input_is_html_escaped(dp, bot, session: MockSession) -> None:
    evil = User(id=5000, is_bot=False, first_name="<b>Хакер</b>", last_name='"&', username=None)
    user = FakeUser(dp, bot, session, evil)
    await user.send("/start")
    await user.press(APPLY)
    await user.send('<a href="https://evil.example">Имя</a>')
    await user.send("x@evil.ru")
    await user.press(callback_of(session.last_sent(5000), "Другое"))
    await user.send("Задача </b><i>с разметкой</i> & <script>alert(1)</script>")
    await user.press(callback_of(session.last_sent(5000), texts.BTN_AGREE))

    summary = session.last_sent(5000).text
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;Имя&lt;/a&gt;" in summary
    assert "<a href" not in summary.replace('<a href="tg://', "")

    await user.press(SUBMIT)
    admin_text = session.last_sent(ADMIN_ID).text
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;Имя&lt;/a&gt;" in admin_text
    assert "&lt;/b&gt;&lt;i&gt;с разметкой&lt;/i&gt; &amp; &lt;script&gt;" in admin_text
    # Имя из Telegram тоже экранировано внутри ссылки-упоминания
    assert '<a href="tg://user?id=5000">&lt;b&gt;Хакер&lt;/b&gt; &quot;&amp;</a>' in admin_text
    assert "Ник: не задан" in admin_text
    # Ника нет ни в профиле, ни в контакте → кнопка по id
    assert (texts.BTN_WRITE_TO_CLIENT, None, "tg://user?id=5000") in buttons(
        session.last_sent(ADMIN_ID)
    )
    # «Сырой» пользовательской разметки в сообщении нет
    assert "<script>" not in admin_text and "<i>" not in admin_text


async def test_reply_url_from_contact_username(dp, bot, session: MockSession) -> None:
    user = FakeUser(dp, bot, session, User(id=5100, is_bot=False, first_name="Иван"))
    await user.send("/start")
    await user.press(APPLY)
    await user.send("Иван")
    await user.send("@ivan_dev")
    await user.press(callback_of(session.last_sent(5100), "Сайт-визитка"))
    await user.send(TASK)
    await user.press(callback_of(session.last_sent(5100), texts.BTN_AGREE))
    await user.press(SUBMIT)
    assert (texts.BTN_WRITE_TO_CLIENT, None, "https://t.me/ivan_dev") in buttons(
        session.last_sent(ADMIN_ID)
    )


async def test_privacy_restricted_button_falls_back(dp, bot, session: MockSession) -> None:
    def hook(method: TelegramMethod) -> None:
        to_admin = isinstance(method, SendMessage) and int(method.chat_id) == ADMIN_ID
        if to_admin and any((b[2] or "").startswith("tg://") for b in buttons(method)):
            raise TelegramBadRequest(method, "Bad Request: BUTTON_USER_PRIVACY_RESTRICTED")
        return None

    session.hook = hook
    user = FakeUser(dp, bot, session, User(id=5200, is_bot=False, first_name="Ольга"))
    await user.send("/start")
    await user.press(APPLY)
    await user.send("Ольга")
    await user.send("olga@mail.ru")
    await user.press(callback_of(session.last_sent(5200), "Лендинг"))
    await user.send(TASK)
    await user.press(callback_of(session.last_sent(5200), texts.BTN_AGREE))
    await user.press(SUBMIT)

    attempts = session.sent(ADMIN_ID)
    assert len(attempts) == 2
    assert [b[0] for b in buttons(attempts[-1])] == [texts.BTN_TAKE]
    assert session.last_sent(5200).text.startswith(ACCEPTED)


async def test_network_failure_retries_then_succeeds(
    client: FakeUser, session: MockSession, sleeps: list[float]
) -> None:
    await fill_until_summary(client, session)
    session.hook = fail_admin_with(
        lambda m: TelegramNetworkError(m, "ClientConnectorError"), times=2
    )
    await client.press(SUBMIT)
    assert len(session.sent(ADMIN_ID)) == 3  # 2 сбоя + успех
    assert sleeps == [0.01, 0.01]  # паузы из настроек между попытками
    assert session.last_sent(CLIENT_ID).text.startswith(ACCEPTED)


async def test_retry_after_is_respected(
    client: FakeUser, session: MockSession, sleeps: list[float]
) -> None:
    await fill_until_summary(client, session)
    session.hook = fail_admin_with(lambda m: TelegramRetryAfter(m, "Too Many Requests", 7), times=1)
    await client.press(SUBMIT)
    assert sleeps == [7.0]
    assert session.last_sent(CLIENT_ID).text.startswith(ACCEPTED)


async def test_delivery_failure_tells_client_and_keeps_summary(
    client: FakeUser, session: MockSession, sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    await fill_until_summary(client, session)
    session.hook = fail_admin_with(lambda m: TelegramNetworkError(m, "ClientConnectorError"))
    caplog.set_level(logging.INFO)
    session.clear()
    await client.press(SUBMIT)

    assert len(session.sent(ADMIN_ID)) == 3  # 3 попытки, затем сдаёмся
    to_client = [m.text for m in session.sent(CLIENT_ID)]
    assert to_client[0] == texts.SEND_FAILED_WITH_CONTACT.format(
        contact="mail@example.com или @example"
    )
    assert "Проверьте заявку" in to_client[1]  # сводка снова с кнопками
    assert any(b[0] == texts.BTN_SUBMIT for b in buttons(session.last_sent(CLIENT_ID)))

    # В логах нет персональных данных
    log = caplog.text
    assert "НЕ доставлена" in log
    for secret in ("Анна", "anna@mail.ru", TASK, "anna_k"):
        assert secret not in log

    # Повторная попытка после восстановления сети проходит
    session.hook = None
    await client.press(SUBMIT)
    assert session.last_sent(CLIENT_ID).text.startswith(ACCEPTED)


async def test_forbidden_is_not_retried(
    client: FakeUser, session: MockSession, sleeps: list[float]
) -> None:
    await fill_until_summary(client, session)
    session.hook = fail_admin_with(
        lambda m: TelegramForbiddenError(m, "Forbidden: bot was blocked")
    )
    await client.press(SUBMIT)
    assert len(session.sent(ADMIN_ID)) == 1
    assert sleeps == []
    assert session.sent(CLIENT_ID)[-2].text.startswith("⚠️ <b>Не получилось отправить заявку</b>")


async def test_double_submit_sends_once(client: FakeUser, session: MockSession) -> None:
    await fill_until_summary(client, session)
    summary = session.last_sent(CLIENT_ID)
    session.slow_chat = ADMIN_ID  # пока идёт отправка администратору, жмём ещё раз
    await asyncio.gather(client.press(SUBMIT, on=summary), client.press(SUBMIT, on=summary))
    assert len(session.sent(ADMIN_ID)) == 1
    # Второе нажатие дождалось очереди и увидело, что заявка уже отправлена
    assert texts.STALE_BUTTON in [a.text for a in session.answers()]
    assert [m.text for m in session.sent(CLIENT_ID)].count(
        session.last_sent(CLIENT_ID).text
    ) == 1  # «Заявка принята» — один раз

    # И позже старая кнопка тоже ничего не делает
    session.slow_chat = None
    await client.press(SUBMIT, on=summary)
    assert len(session.sent(ADMIN_ID)) == 1
    assert session.answers()[-1].text == texts.STALE_BUTTON


async def test_fast_messages_are_processed_in_order(client: FakeUser, session: MockSession) -> None:
    """Имя и почта, отправленные подряд, не должны «обогнать» друг друга."""
    await client.send("/start")
    await client.press(APPLY)
    session.slow_chat = CLIENT_ID  # ответы бота медленные — апдейты пересекаются
    await asyncio.gather(client.send("Анна"), client.send("anna@mail.ru"))
    assert [m.text for m in session.sent(CLIENT_ID)][-2:] == [
        texts.ASK_CONTACT,
        texts.ASK_PROJECT_TYPE,
    ]


async def test_double_tap_on_button_moves_one_step(client: FakeUser, session: MockSession) -> None:
    await client.send("/start")
    await client.press(APPLY)
    await client.send("Анна")
    await client.send("anna@mail.ru")
    question = session.last_sent(CLIENT_ID)
    landing = callback_of(question, "Лендинг")
    session.slow_chat = CLIENT_ID
    session.clear()
    await asyncio.gather(client.press(landing, on=question), client.press(landing, on=question))
    asked = [m.text for m in session.sent(CLIENT_ID)]
    assert len(asked) == 1 and asked[0].startswith("<b>Шаг 4 из 5</b>")
    assert texts.UNEXPECTED_ERROR not in asked


async def test_resubmit_after_failure_keeps_number(
    client: FakeUser, session: MockSession, sleeps: list[float]
) -> None:
    """Повтор после сбоя приходит с тем же номером — дубль у администратора узнаваем."""
    await fill_until_summary(client, session)
    session.hook = fail_admin_with(lambda m: TelegramNetworkError(m, "timeout"))
    await client.press(SUBMIT)
    first = session.sent(ADMIN_ID)[0].text.split("\n")[0]
    session.hook = None
    await client.press(SUBMIT)
    assert session.sent(ADMIN_ID)[-1].text.split("\n")[0] == first
    number = first.removeprefix("<b>Новая заявка № ").removesuffix("</b>")
    assert f"<code>{number}</code>" in session.last_sent(CLIENT_ID).text

    # Следующая заявка — уже с новым номером
    await fill_until_summary(client, session)
    await client.press(SUBMIT)
    assert session.sent(ADMIN_ID)[-1].text.split("\n")[0] != first


async def test_rate_limit_per_user(client: FakeUser, session: MockSession) -> None:
    for _ in range(3):
        await fill_until_summary(client, session)
        await client.press(SUBMIT)
    assert len(session.sent(ADMIN_ID)) == 3

    await client.send("/start")
    await client.press(APPLY)
    assert session.last_sent(CLIENT_ID).text.startswith("⏳ <b>Вы уже отправили несколько заявок")
    assert len(session.sent(ADMIN_ID)) == 3


async def test_failed_delivery_does_not_consume_limit(
    client: FakeUser, session: MockSession
) -> None:
    session.hook = fail_admin_with(lambda m: TelegramForbiddenError(m, "Forbidden"))
    for _ in range(4):
        await fill_until_summary(client, session)
        await client.press(SUBMIT)
    session.hook = None
    await fill_until_summary(client, session)
    await client.press(SUBMIT)
    assert session.last_sent(CLIENT_ID).text.startswith(ACCEPTED)


async def test_take_in_work_by_admin(
    client: FakeUser, admin: FakeUser, session: MockSession
) -> None:
    await fill_until_summary(client, session)
    await client.press(SUBMIT)
    app_message = session.last_sent(ADMIN_ID)
    take = callback_of(app_message, texts.BTN_TAKE)

    # Чужой пользователь (например, если сообщение переслали) — отказ
    session.clear()
    await client.press(take, on=app_message)
    assert session.answers()[-1].text == texts.ADMIN_TAKE_DENIED
    assert session.answers()[-1].show_alert
    assert session.edits() == []

    await admin.press(take, on=app_message)
    edit = session.edits()[-1]
    assert isinstance(edit, EditMessageText)
    assert "✅ <b>В работе</b> с " in edit.text
    assert "Новая заявка №" in edit.text
    # Осталась только кнопка-ссылка «Написать клиенту»
    remaining = [b.text for row in edit.reply_markup.inline_keyboard for b in row]
    assert remaining == [texts.BTN_WRITE_TO_CLIENT]
    # Базы в этом тесте нет (NullRepository) — всплывашка честно говорит, что клиенту
    # не сообщено (этап 2в).
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_NO_CLIENT_NOTICE


async def test_group_chats_are_ignored(dp, bot, session: MockSession) -> None:
    member = FakeUser(
        dp,
        bot,
        session,
        User(id=6000, is_bot=False, first_name="Гость"),
        chat_type="supergroup",
        chat_id=-100123,
    )
    await member.send("/start")
    await member.send("/help")
    await member.press(APPLY)
    assert session.requests == []


async def test_error_handler_answers_neutrally(
    client: FakeUser, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    await client.send("/start")
    await client.press(APPLY)
    await client.send("Анна")
    await client.send("anna@mail.ru")
    project_question = session.last_sent(CLIENT_ID)

    def hook(method: TelegramMethod) -> None:
        if isinstance(method, EditMessageText):
            raise RuntimeError("boom")
        return None

    session.hook = hook
    await client.press(callback_of(project_question, "Лендинг"))
    assert session.last_sent(CLIENT_ID).text == texts.UNEXPECTED_ERROR
    assert "RuntimeError" in caplog.text


async def test_cancel_button_works_on_every_step(client: FakeUser, session: MockSession) -> None:
    cancel = FormCallback(action=FormAction.CANCEL).pack()
    steps = [
        lambda: None,
        lambda: client.send("Анна"),
        lambda: client.send("anna@mail.ru"),
        lambda: client.press(callback_of(session.last_sent(CLIENT_ID), "Лендинг")),
        lambda: client.send(TASK),
        lambda: client.press(callback_of(session.last_sent(CLIENT_ID), texts.BTN_AGREE)),
    ]
    for depth in range(len(steps)):
        await client.send("/start")
        await client.press(APPLY)
        for step in steps[1 : depth + 1]:
            await step()
        await client.press(cancel)
        assert session.last_sent(CLIENT_ID).text == texts.CANCELLED, depth
    assert session.sent(ADMIN_ID) == []


def _as_received(sent: SendMessage, chat_id: int) -> Message:
    """Сообщение администратора таким, каким его вернёт Telegram: текст + entities."""
    text, entities = as_telegram_sees(sent.text)
    return Message(
        message_id=77,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type="private"),
        text=text,
        entities=entities,
        reply_markup=sent.reply_markup,
    )


async def test_take_keeps_markup_and_escaping_from_entities(
    dp, bot, session: MockSession, admin: FakeUser
) -> None:
    evil = User(id=5300, is_bot=False, first_name="<i>Ева</i>", username=None)
    user = FakeUser(dp, bot, session, evil)
    await user.send("/start")
    await user.press(APPLY)
    await user.send("Ева & <b>Ко</b>")
    await user.send("eva@mail.ru")
    await user.press(callback_of(session.last_sent(5300), "Другое"))
    await user.send("Задача </b><script>alert(1)</script> 😀 — проверить разметку")
    await user.press(callback_of(session.last_sent(5300), texts.BTN_AGREE))
    await user.press(SUBMIT)
    sent = session.last_sent(ADMIN_ID)
    received = _as_received(sent, ADMIN_ID)

    await admin.press(callback_of(sent, texts.BTN_TAKE), message=received)
    edited = session.edits()[-1].text
    # Разметка восстановлена из entities, пользовательский ввод снова экранирован
    plain_before, entities_before = as_telegram_sees(sent.text)
    plain_after, entities_after = as_telegram_sees(edited)
    assert plain_after.startswith(plain_before + "\n\n✅ В работе с ")
    assert entities_after[: len(entities_before)] == entities_before
    assert "&lt;script&gt;" in edited and "<script>" not in edited
    assert '<a href="tg://user?id=5300">&lt;i&gt;Ева&lt;/i&gt;</a>' in edited

    # Повторное нажатие (Telegram пришлёт уже изменённое сообщение) — без второй отметки
    after = Message(
        message_id=77,
        date=datetime.now(UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        text=plain_after,
        entities=entities_after,
        reply_markup=session.edits()[-1].reply_markup,
    )
    edits_before = len(session.edits())
    await admin.press(callback_of(sent, texts.BTN_TAKE), message=after)
    assert len(session.edits()) == edits_before
    assert session.answers()[-1].text == texts.ADMIN_TAKEN_ALERT


async def test_admin_message_fits_telegram_limit(dp, bot, session: MockSession) -> None:
    """Худший случай по всем полям: после разбора HTML — не больше 4096 UTF-16 символов."""
    limit = 4096
    astral = "😀"  # 2 единицы UTF-16 на символ
    worst = User(
        id=9_999_999_999,
        is_bot=False,
        first_name=astral * 64,
        last_name=astral * 64,
        username="u" * 32,
    )
    user = FakeUser(dp, bot, session, worst)
    await user.send("/start")
    await user.press(APPLY)
    await user.send(astral * 80)
    await user.send("a" * 64 + "@" + astral * 52 + ".ru")  # 120 символов
    await user.press(callback_of(session.last_sent(worst.id), "Интернет-магазин"))
    await user.send((astral + "&<") * 333 + astral)  # 1000 символов
    await user.press(callback_of(session.last_sent(worst.id), texts.BTN_AGREE))
    summary, _ = as_telegram_sees(session.last_sent(worst.id).text)
    assert len(summary.encode("utf-16-le")) // 2 <= limit
    await user.press(SUBMIT)
    sent = session.last_sent(ADMIN_ID)
    plain, _ = as_telegram_sees(sent.text)
    # + отметка «В работу», которая дописывается позже
    mark, _ = as_telegram_sees("\n\n" + texts.ADMIN_TAKEN_MARK.format(taken_at="31.12.2026 23:59"))
    assert len((plain + mark).encode("utf-16-le")) // 2 <= limit


async def test_blocked_bot_is_not_logged_as_error(
    client: FakeUser, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    def hook(method: TelegramMethod) -> None:
        # Заблокированному боту Telegram не даёт отправить ничего: ни текст, ни фото.
        to_client = isinstance(method, SendMessage | SendPhoto)
        if to_client and int(method.chat_id) == CLIENT_ID:
            raise TelegramForbiddenError(method, "Forbidden: bot was blocked by the user")
        return None

    session.hook = hook
    caplog.set_level(logging.INFO)
    await client.send("/start")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "заблокировал бота" in caplog.text


async def test_forged_admin_callback_cannot_inject_log_lines(
    client: FakeUser, session: MockSession, caplog: pytest.LogCaptureFixture
) -> None:
    await client.send("/start")
    await client.press("adm:take:x\nCRITICAL fake line")
    assert session.answers()[-1].text == texts.ADMIN_TAKE_DENIED
    assert all("\n" not in r.getMessage() for r in caplog.records)
