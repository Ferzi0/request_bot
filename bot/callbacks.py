"""Фабрики callback_data.

CallbackData даёт типизированный разбор вместо ручного split(":"), а префикс
отделяет «пространства» кнопок друг от друга. Лимит Telegram — 64 байта,
поэтому значения короткие (латинские коды, а не русские подписи).
"""

from enum import StrEnum

from aiogram.filters.callback_data import CallbackData

from bot.models import ProjectType


class FormAction(StrEnum):
    APPLY = "apply"
    CANCEL = "cancel"
    USE_TG_NAME = "tg_name"
    USE_TG_USERNAME = "tg_user"
    AGREE = "agree"
    SUBMIT = "submit"
    EDIT = "edit"
    BACK_TO_SUMMARY = "summary"


class EditField(StrEnum):
    NAME = "name"
    CONTACT = "contact"
    PROJECT_TYPE = "ptype"
    TASK = "task"


class FormCallback(CallbackData, prefix="f"):
    action: FormAction


class ProjectTypeCallback(CallbackData, prefix="pt"):
    value: ProjectType


class EditFieldCallback(CallbackData, prefix="ef"):
    field: EditField


class AdminCallback(CallbackData, prefix="adm"):
    action: str  # пока только "take"; строка, чтобы добавлять действия без миграций
    app_id: str


class PanelAction(StrEnum):
    HOME = "h"  # главный экран со счётчиками
    LIST = "l"  # страница списка
    CARD = "c"  # карточка заявки
    ASK_CANCEL = "q"  # «Отменить?» — подтверждение
    SET = "s"  # сменить статус на target


# Фильтр списка: код статуса или ALL. Хранится в кнопках карточки, чтобы
# «‹ К списку» вернул на ту же страницу того же раздела.
LIST_ALL = "all"


class PanelCallback(CallbackData, prefix="p"):
    """Кнопки админ-панели. Поля однобуквенные: лимит callback_data — 64 байта.

    Самая длинная кнопка — «p:s:cancelled:999:261019-ABCD:cancelled» — около 40 байт.
    """

    a: PanelAction
    f: str = LIST_ALL  # фильтр списка (статус или all)
    p: int = 0  # номер страницы, с нуля
    i: str = ""  # номер заявки
    t: str = ""  # целевой статус для SET
