"""Inline-клавиатуры. Только разметка — никакой логики и проверок."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot import texts
from bot.callbacks import (
    AdminCallback,
    EditField,
    EditFieldCallback,
    FormAction,
    FormCallback,
    ProjectTypeCallback,
)
from bot.models import ProjectType


def _button(text: str, action: FormAction) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=FormCallback(action=action).pack())


def _cancel_row() -> list[InlineKeyboardButton]:
    return [_button(texts.BTN_CANCEL, FormAction.CANCEL)]


def apply_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button(texts.BTN_APPLY, FormAction.APPLY)]])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_cancel_row()])


def name_keyboard(*, offer_tg_name: bool) -> InlineKeyboardMarkup:
    rows = [[_button(texts.BTN_USE_TG_NAME, FormAction.USE_TG_NAME)]] if offer_tg_name else []
    return InlineKeyboardMarkup(inline_keyboard=[*rows, _cancel_row()])


def contact_keyboard() -> InlineKeyboardMarkup:
    # Кнопку показываем всегда: если ника нет, хендлер вежливо об этом скажет.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(texts.BTN_USE_TG_USERNAME, FormAction.USE_TG_USERNAME)],
            _cancel_row(),
        ]
    )


def project_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for project_type in ProjectType:
        builder.button(
            text=project_type.label,
            callback_data=ProjectTypeCallback(value=project_type),
        )
    builder.adjust(2)  # по две в ряд — подписи короткие, так компактнее
    builder.row(*_cancel_row())
    return builder.as_markup()


def consent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _button(texts.BTN_AGREE, FormAction.AGREE),
                _button(texts.BTN_CANCEL, FormAction.CANCEL),
            ]
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_button(texts.BTN_SUBMIT, FormAction.SUBMIT)],
            [
                _button(texts.BTN_EDIT, FormAction.EDIT),
                _button(texts.BTN_CANCEL, FormAction.CANCEL),
            ],
        ]
    )


def edit_field_keyboard() -> InlineKeyboardMarkup:
    labels = {
        EditField.NAME: texts.BTN_FIELD_NAME,
        EditField.CONTACT: texts.BTN_FIELD_CONTACT,
        EditField.PROJECT_TYPE: texts.BTN_FIELD_PROJECT_TYPE,
        EditField.TASK: texts.BTN_FIELD_TASK,
    }
    builder = InlineKeyboardBuilder()
    for field, label in labels.items():
        builder.button(text=label, callback_data=EditFieldCallback(field=field))
    builder.adjust(2)
    builder.row(_button(texts.BTN_BACK_TO_SUMMARY, FormAction.BACK_TO_SUMMARY))
    return builder.as_markup()


def admin_application_keyboard(
    app_id: str, reply_url: str | None, *, with_take: bool = True
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if reply_url:
        rows.append([InlineKeyboardButton(text=texts.BTN_WRITE_TO_CLIENT, url=reply_url)])
    if with_take:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.BTN_TAKE,
                    callback_data=AdminCallback(action="take", app_id=app_id).pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
