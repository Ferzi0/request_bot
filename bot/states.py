"""Состояния диалога заявки (FSM)."""

from aiogram.fsm.state import State, StatesGroup


class ApplicationForm(StatesGroup):
    name = State()
    contact = State()
    project_type = State()
    task = State()
    consent = State()
    confirm = State()  # сводка: «Отправить» / «Изменить» / «Отмена»
    choose_field = State()  # «Изменить»: какое поле поправить
