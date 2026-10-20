"""Диалог заявки: имя → контакт → тип проекта → задача → согласие → сводка → отправка.

Хендлеры здесь тонкие: проверка ввода — в services/validation, отправка —
в services/notifier, тексты — в texts. Зависимости (limits, notifier, …)
приходят из workflow_data диспетчера по имени параметра (см. bot/app.py).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message, User

from bot import texts
from bot.callbacks import (
    EditField,
    EditFieldCallback,
    FormAction,
    FormCallback,
    ProjectTypeCallback,
)
from bot.config import Settings
from bot.drafts import load_draft, reset_form, save_draft
from bot.handlers.common import drop_keyboard
from bot.keyboards import (
    cancel_keyboard,
    confirm_keyboard,
    consent_keyboard,
    contact_keyboard,
    edit_field_keyboard,
    name_keyboard,
    project_type_keyboard,
)
from bot.models import Applicant, Application, ApplicationDraft, new_application_id
from bot.services.formatting import format_consent, format_send_failed, format_summary
from bot.services.media import PhotoSender, Picture
from bot.services.notifier import AdminNotifier
from bot.services.rate_limit import SlidingWindowLimiter
from bot.services.storage import ApplicationRepository
from bot.services.storage_alerts import StorageAlerts, mark_delivered_quietly, save_quietly
from bot.services.validation import (
    InvalidInputError,
    Limits,
    sanitize,
    validate_contact,
    validate_name,
    validate_task,
)
from bot.states import ApplicationForm as Form

logger = logging.getLogger(__name__)

TEXT_STEPS = StateFilter(Form.name, Form.contact, Form.task)
BUTTON_STEPS = StateFilter(Form.project_type, Form.consent, Form.confirm, Form.choose_field)


# ---------- вопросы шагов ----------
# Каждый ask_* ставит состояние и задаёт вопрос. Принимают Message, куда отвечать:
# это либо сообщение пользователя, либо наше сообщение с нажатой кнопкой.


def _tg_name(user: User, limits: Limits) -> str | None:
    """Имя из Telegram, если оно проходит те же проверки, что и ручной ввод."""
    try:
        return validate_name(user.first_name, limits)
    except InvalidInputError:
        return None


async def ask_name(target: Message, user: User, state: FSMContext, limits: Limits) -> None:
    await state.set_state(Form.name)
    offer = _tg_name(user, limits) is not None
    await target.answer(texts.ASK_NAME, reply_markup=name_keyboard(offer_tg_name=offer))


async def ask_contact(target: Message, state: FSMContext) -> None:
    await state.set_state(Form.contact)
    await target.answer(texts.ASK_CONTACT, reply_markup=contact_keyboard())


async def ask_project_type(target: Message, state: FSMContext) -> None:
    await state.set_state(Form.project_type)
    await target.answer(texts.ASK_PROJECT_TYPE, reply_markup=project_type_keyboard())


async def ask_task(target: Message, state: FSMContext, limits: Limits) -> None:
    await state.set_state(Form.task)
    await target.answer(
        texts.ASK_TASK.format(limit=texts.chars(limits.task_max)), reply_markup=cancel_keyboard()
    )


async def ask_consent(target: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(Form.consent)
    await target.answer(format_consent(settings.policy_url), reply_markup=consent_keyboard())


async def show_summary(target: Message, state: FSMContext, draft: ApplicationDraft) -> None:
    draft = draft.model_copy(update={"editing": False})
    await save_draft(state, draft)
    await state.set_state(Form.confirm)
    await target.answer(format_summary(draft), reply_markup=confirm_keyboard())


async def _after_field(
    target: Message,
    state: FSMContext,
    draft: ApplicationDraft,
    next_step: State,
    limits: Limits,
    settings: Settings,
) -> None:
    """После ввода поля: в режиме «Изменить» — к сводке, иначе — следующий шаг."""
    await save_draft(state, draft)
    if draft.editing and draft.is_complete():
        await show_summary(target, state, draft)
    elif next_step == Form.contact:
        await ask_contact(target, state)
    elif next_step == Form.project_type:
        await ask_project_type(target, state)
    elif next_step == Form.task:
        await ask_task(target, state, limits)
    else:
        await ask_consent(target, state, settings)


# ---------- старт заявки ----------


async def cb_apply(
    callback: CallbackQuery,
    state: FSMContext,
    limits: Limits,
    limiter: SlidingWindowLimiter,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    await drop_keyboard(callback)
    if wait := limiter.retry_after(callback.from_user.id):
        await callback.message.answer(texts.RATE_LIMITED.format(minutes=math.ceil(wait / 60)))
        return
    await reset_form(state)
    logger.info("form: user=%s started", callback.from_user.id)
    await ask_name(callback.message, callback.from_user, state, limits)


# ---------- шаг 1: имя ----------


async def step_name(
    message: Message, state: FSMContext, limits: Limits, settings: Settings
) -> None:
    try:
        name = validate_name(message.text or "", limits)  # текст гарантирует фильтр F.text
    except InvalidInputError as err:
        await message.answer(err.message, reply_markup=cancel_keyboard())
        return
    draft = (await load_draft(state)).model_copy(update={"name": name})
    await _after_field(message, state, draft, Form.contact, limits, settings)


async def cb_use_tg_name(
    callback: CallbackQuery,
    state: FSMContext,
    limits: Limits,
    settings: Settings,
) -> None:
    name = _tg_name(callback.from_user, limits)
    if name is None:
        await callback.answer(texts.TG_NAME_UNUSABLE, show_alert=True)
        return
    await callback.answer()
    await drop_keyboard(callback)
    if not isinstance(callback.message, Message):
        return
    draft = (await load_draft(state)).model_copy(update={"name": name})
    await _after_field(callback.message, state, draft, Form.contact, limits, settings)


# ---------- шаг 2: контакт ----------


async def step_contact(
    message: Message,
    state: FSMContext,
    limits: Limits,
    settings: Settings,
) -> None:
    try:
        contact = validate_contact(message.text or "", limits)
    except InvalidInputError as err:
        await message.answer(err.message, reply_markup=contact_keyboard())
        return
    draft = (await load_draft(state)).model_copy(update={"contact": contact})
    await _after_field(message, state, draft, Form.project_type, limits, settings)


async def cb_use_tg_username(
    callback: CallbackQuery,
    state: FSMContext,
    limits: Limits,
    settings: Settings,
) -> None:
    username = callback.from_user.username
    if not username:
        # Оставляем человека на этом же шаге: пусть введёт почту или ник вручную.
        await callback.answer(texts.NO_USERNAME, show_alert=True)
        return
    await callback.answer()
    await drop_keyboard(callback)
    if not isinstance(callback.message, Message):
        return
    draft = (await load_draft(state)).model_copy(update={"contact": f"@{username}"})
    await _after_field(callback.message, state, draft, Form.project_type, limits, settings)


# ---------- шаг 3: тип проекта ----------


async def cb_project_type(
    callback: CallbackQuery,
    callback_data: ProjectTypeCallback,
    state: FSMContext,
    limits: Limits,
    settings: Settings,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    # Вместо кнопок оставляем в истории выбранный вариант — так переписка читается.
    await callback.message.edit_text(
        texts.PROJECT_TYPE_CHOSEN.format(project_type=callback_data.value.label),
        reply_markup=None,
    )
    draft = (await load_draft(state)).model_copy(update={"project_type": callback_data.value})
    await _after_field(callback.message, state, draft, Form.task, limits, settings)


# ---------- шаг 4: задача ----------


async def step_task(
    message: Message, state: FSMContext, limits: Limits, settings: Settings
) -> None:
    try:
        task = validate_task(message.text or "", limits)
    except InvalidInputError as err:
        await message.answer(err.message, reply_markup=cancel_keyboard())
        return
    logger.info("form: user=%s task length=%d", message.chat.id, len(task))
    draft = (await load_draft(state)).model_copy(update={"task": task})
    await _after_field(message, state, draft, Form.consent, limits, settings)


# ---------- ввод не того типа ----------


async def step_not_text(message: Message) -> None:
    # Стикер, фото, голосовое и т. п. на шаге, где ждём текст.
    await message.answer(texts.TEXT_ONLY, reply_markup=cancel_keyboard())


async def step_expects_button(message: Message) -> None:
    await message.answer(texts.USE_BUTTONS)


# ---------- шаг 5: согласие → сводка ----------


async def cb_agree(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await drop_keyboard(callback)
    if not isinstance(callback.message, Message):
        return
    draft = (await load_draft(state)).model_copy(update={"consent": True})
    await show_summary(callback.message, state, draft)


# ---------- «Изменить» ----------


async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await drop_keyboard(callback)
    if not isinstance(callback.message, Message):
        return
    await state.set_state(Form.choose_field)
    await callback.message.answer(texts.CHOOSE_FIELD, reply_markup=edit_field_keyboard())


async def cb_edit_field(
    callback: CallbackQuery,
    callback_data: EditFieldCallback,
    state: FSMContext,
    limits: Limits,
) -> None:
    await callback.answer()
    await drop_keyboard(callback)
    if not isinstance(callback.message, Message):
        return
    draft = (await load_draft(state)).model_copy(update={"editing": True})
    await save_draft(state, draft)
    target = callback.message
    match callback_data.field:
        case EditField.NAME:
            await ask_name(target, callback.from_user, state, limits)
        case EditField.CONTACT:
            await ask_contact(target, state)
        case EditField.PROJECT_TYPE:
            await ask_project_type(target, state)
        case EditField.TASK:
            await ask_task(target, state, limits)


async def cb_back_to_summary(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await drop_keyboard(callback)
    if isinstance(callback.message, Message):
        await show_summary(callback.message, state, await load_draft(state))


# ---------- отправка ----------


async def cb_submit(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    notifier: AdminNotifier,
    limiter: SlidingWindowLimiter,
    repository: ApplicationRepository,
    storage_alerts: StorageAlerts,
    media: PhotoSender,
) -> None:
    # Второе нажатие «Отправить» ждёт в очереди (SimpleEventIsolation), а когда
    # дойдёт, состояние уже сброшено — его перехватит any_callback («неактуальна»).
    user = callback.from_user
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if wait := limiter.retry_after(user.id):
        await callback.message.answer(texts.RATE_LIMITED.format(minutes=math.ceil(wait / 60)))
        return
    await drop_keyboard(callback)

    now = datetime.now(UTC)
    draft = await load_draft(state)
    if draft.app_id is None:
        # Номер выдаётся один раз на черновик: повтор после сбоя придёт администратору
        # с тем же номером, и возможный дубль сразу видно.
        draft = draft.model_copy(update={"app_id": new_application_id(now, settings.tz)})
        await save_draft(state, draft)
    applicant = Applicant(
        user_id=user.id,
        username=user.username,
        full_name=sanitize(user.full_name) or str(user.id),
    )
    app = Application.from_draft(draft, applicant, now=now)
    logger.info(
        "submit: user=%s app=%s source=%s task_len=%d",
        user.id,
        app.id,
        app.source.value,
        len(app.task),
    )
    # Сохраняем ДО отправки: если Telegram недоступен, заявка всё равно останется
    # в базе (с delivered_at = NULL). Сбой базы доставку не останавливает.
    save_error = await save_quietly(repository, app)
    if await notifier.notify(bot, app):
        # Всё о базе — до ответа клиенту: если клиент успел заблокировать бота,
        # ответ ему упадёт, а отметка и уведомление администратору должны остаться.
        if save_error:
            # Порядок важен для текста: заявка уже у администратора («в сообщении выше»).
            await storage_alerts.save_failed(bot, app.id, save_error)
        elif mark_error := await mark_delivered_quietly(repository, app.id, datetime.now(UTC)):
            await storage_alerts.mark_failed(bot, app.id, mark_error)
        limiter.hit(user.id)  # считаем только доставленные: сбой не съедает лимит
        await reset_form(state)
        # Подтверждение — только ПОСЛЕ доставки администратору. Сбой картинки сюда
        # не возвращается: PhotoSender сам отправит тот же текст обычным сообщением.
        await media.send(
            bot, callback.message.chat.id, Picture.ACCEPTED, texts.SENT.format(app_id=app.id)
        )
        return

    # Не доставлено: честно говорим и оставляем сводку, чтобы можно было повторить.
    await callback.message.answer(format_send_failed(settings.fallback_contact))
    await show_summary(callback.message, state, draft)


def create_router() -> Router:
    """Шаги заявки. Порядок важен: точные фильтры раньше «ловушек» вроде step_not_text."""
    router = Router(name="application")
    router.callback_query.register(cb_apply, FormCallback.filter(F.action == FormAction.APPLY))
    router.message.register(step_name, Form.name, F.text)
    router.callback_query.register(
        cb_use_tg_name, Form.name, FormCallback.filter(F.action == FormAction.USE_TG_NAME)
    )
    router.message.register(step_contact, Form.contact, F.text)
    router.callback_query.register(
        cb_use_tg_username,
        Form.contact,
        FormCallback.filter(F.action == FormAction.USE_TG_USERNAME),
    )
    router.callback_query.register(cb_project_type, Form.project_type, ProjectTypeCallback.filter())
    router.message.register(step_task, Form.task, F.text)
    router.message.register(step_not_text, TEXT_STEPS)
    router.message.register(step_expects_button, BUTTON_STEPS)
    router.callback_query.register(
        cb_agree, Form.consent, FormCallback.filter(F.action == FormAction.AGREE)
    )
    router.callback_query.register(
        cb_edit, Form.confirm, FormCallback.filter(F.action == FormAction.EDIT)
    )
    router.callback_query.register(cb_edit_field, Form.choose_field, EditFieldCallback.filter())
    router.callback_query.register(
        cb_back_to_summary,
        Form.choose_field,
        FormCallback.filter(F.action == FormAction.BACK_TO_SUMMARY),
    )
    router.callback_query.register(
        cb_submit, Form.confirm, FormCallback.filter(F.action == FormAction.SUBMIT)
    )
    return router
