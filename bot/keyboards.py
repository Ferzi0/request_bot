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
    PanelAction,
    PanelCallback,
    ProjectTypeCallback,
)
from bot.models import ALLOWED_TRANSITIONS, ApplicationRecord, ApplicationStatus, ProjectType


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


# ---------- админ-панель ----------


def _panel(text: str, data: PanelCallback) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data.pack())


def _home_button() -> InlineKeyboardButton:
    return _panel(texts.PANEL_BTN_HOME, PanelCallback(a=PanelAction.HOME))


def panel_home_keyboard(counts: dict[ApplicationStatus, int]) -> InlineKeyboardMarkup:
    labels = {
        ApplicationStatus.NEW: texts.PANEL_BTN_NEW,
        ApplicationStatus.IN_WORK: texts.PANEL_BTN_IN_WORK,
        ApplicationStatus.DONE: texts.PANEL_BTN_DONE,
        ApplicationStatus.CANCELLED: texts.PANEL_BTN_CANCELLED,
    }
    builder = InlineKeyboardBuilder()
    for status, label in labels.items():
        builder.add(
            _panel(
                label.format(count=counts.get(status, 0)),
                PanelCallback(a=PanelAction.LIST, f=status.value),
            )
        )
    builder.add(_panel(texts.PANEL_BTN_ALL, PanelCallback(a=PanelAction.LIST)))
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def panel_home_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_home_button()]])


def panel_list_keyboard(
    records: list[ApplicationRecord], list_filter: str, page: int, *, has_next: bool
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            _panel(
                f"{record.status.icon} {record.application.id}",
                PanelCallback(a=PanelAction.CARD, f=list_filter, p=page, i=record.application.id),
            )
        ]
        for record in records
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            _panel(
                texts.PANEL_BTN_PREV, PanelCallback(a=PanelAction.LIST, f=list_filter, p=page - 1)
            )
        )
    if has_next:
        nav.append(
            _panel(
                texts.PANEL_BTN_NEXT, PanelCallback(a=PanelAction.LIST, f=list_filter, p=page + 1)
            )
        )
    if nav:
        rows.append(nav)
    rows.append([_home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Подписи кнопок смены статуса. Какие из них показать — решает ALLOWED_TRANSITIONS.
_TRANSITION_BUTTONS: dict[ApplicationStatus, str] = {
    ApplicationStatus.IN_WORK: texts.PANEL_BTN_TAKE,
    ApplicationStatus.DONE: texts.PANEL_BTN_DONE_ACTION,
    ApplicationStatus.CANCELLED: texts.PANEL_BTN_CANCEL_ACTION,
    ApplicationStatus.NEW: texts.PANEL_BTN_REOPEN,
}


def panel_card_keyboard(
    record: ApplicationRecord, reply_url: str | None, list_filter: str, page: int
) -> InlineKeyboardMarkup:
    app_id = record.application.id
    rows: list[list[InlineKeyboardButton]] = []
    if reply_url:
        rows.append([InlineKeyboardButton(text=texts.BTN_WRITE_TO_CLIENT, url=reply_url)])
    actions: list[InlineKeyboardButton] = []
    for target in ALLOWED_TRANSITIONS[record.status]:
        # Отмена — через экран подтверждения, чтобы не отменить случайным нажатием.
        action = (
            PanelAction.ASK_CANCEL if target is ApplicationStatus.CANCELLED else PanelAction.SET
        )
        data = PanelCallback(a=action, f=list_filter, p=page, i=app_id, t=target.value)
        actions.append(_panel(_TRANSITION_BUTTONS[target], data))
    if actions:
        rows.append(actions)
    rows.append(
        [
            _panel(
                texts.PANEL_BTN_BACK_TO_LIST,
                PanelCallback(a=PanelAction.LIST, f=list_filter, p=page),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def panel_confirm_cancel_keyboard(app_id: str, list_filter: str, page: int) -> InlineKeyboardMarkup:
    yes = PanelCallback(
        a=PanelAction.SET, f=list_filter, p=page, i=app_id, t=ApplicationStatus.CANCELLED.value
    )
    no = PanelCallback(a=PanelAction.CARD, f=list_filter, p=page, i=app_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_panel(texts.PANEL_BTN_CONFIRM_CANCEL, yes), _panel(texts.PANEL_BTN_KEEP, no)]
        ]
    )
