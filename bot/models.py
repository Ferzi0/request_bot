"""Доменные модели: заявка и её черновик.

Модели не знают про Telegram-разметку и хендлеры — их можно сохранить
в БД, отправить на почту или в другой канал без изменений.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict


class ProjectType(StrEnum):
    """Тип проекта. Значение — короткий код для callback_data (лимит 64 байта)."""

    SITE = "site"
    LANDING = "landing"
    TG_BOT = "bot"
    WEB_SERVICE = "service"
    SHOP = "shop"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _PROJECT_LABELS[self]


# Подписи совпадают со значениями формы на сайте (index.html, #brief-form).
_PROJECT_LABELS: dict[ProjectType, str] = {
    ProjectType.SITE: "Сайт-визитка",
    ProjectType.LANDING: "Лендинг",
    ProjectType.TG_BOT: "Telegram-бот",
    ProjectType.WEB_SERVICE: "Веб-сервис",
    ProjectType.SHOP: "Интернет-магазин",
    ProjectType.OTHER: "Другое",
}


class Source(StrEnum):
    """Откуда пришёл человек: по ссылке с сайта (`/start site`) или сам."""

    SITE = "site"
    BOT = "bot"

    @property
    def label(self) -> str:
        return "сайт" if self is Source.SITE else "бот"


class Applicant(BaseModel):
    """Telegram-данные отправителя: по ним можно ответить, даже если указана почта."""

    model_config = ConfigDict(frozen=True)

    user_id: int
    username: str | None = None
    full_name: str


class ApplicationDraft(BaseModel):
    """Черновик заявки, пока человек проходит шаги. Хранится в данных FSM."""

    source: Source = Source.BOT
    name: str | None = None
    contact: str | None = None
    project_type: ProjectType | None = None
    task: str | None = None
    consent: bool = False
    # Режим «Изменить»: после ввода поля вернуться к сводке, а не идти дальше по шагам.
    editing: bool = False
    # Номер заявки — выдаётся при первой попытке отправки и сохраняется при повторах.
    app_id: str | None = None

    def is_complete(self) -> bool:
        return all((self.name, self.contact, self.project_type, self.task, self.consent))


class Application(BaseModel):
    """Готовая заявка: все поля проверены и очищены."""

    model_config = ConfigDict(frozen=True)

    id: str
    created_at: datetime
    source: Source
    name: str
    contact: str
    project_type: ProjectType
    task: str
    applicant: Applicant

    @classmethod
    def from_draft(
        cls, draft: ApplicationDraft, applicant: Applicant, now: datetime
    ) -> Application:
        name, contact, project_type, task = (
            draft.name,
            draft.contact,
            draft.project_type,
            draft.task,
        )
        app_id = draft.app_id  # выдаётся хендлером при первой попытке отправки
        if not (app_id and name and contact and project_type and task and draft.consent):
            raise ValueError("draft is incomplete")
        return cls(
            id=app_id,
            created_at=now,
            source=draft.source,
            name=name,
            contact=contact,
            project_type=project_type,
            task=task,
            applicant=applicant,
        )


def new_application_id(now: datetime, tz: ZoneInfo) -> str:
    """Номер заявки: дата + 4 случайных hex-символа, например 261017-3F9A.

    Дата — по местному поясу (TIMEZONE), а не UTC: иначе заявка, отправленная
    в 02:00 по Челябинску, получила бы вчерашнюю дату в номере.
    Счётчик в памяти обнулялся бы при перезапуске и давал повторы; случайный
    хвост уникален на практике и не раскрывает, сколько всего заявок.
    """
    return f"{now.astimezone(tz):%y%m%d}-{secrets.token_hex(2).upper()}"


class ApplicationStatus(StrEnum):
    """Статус заявки в базе. Значение — код в столбце status и в callback_data."""

    NEW = "new"
    IN_WORK = "in_work"
    DONE = "done"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return _STATUS_LABELS[self][1]

    @property
    def icon(self) -> str:
        return _STATUS_LABELS[self][0]


_STATUS_LABELS: dict[ApplicationStatus, tuple[str, str]] = {
    ApplicationStatus.NEW: ("🆕", "Новая"),
    ApplicationStatus.IN_WORK: ("🔧", "В работе"),
    ApplicationStatus.DONE: ("✅", "Выполнена"),
    ApplicationStatus.CANCELLED: ("🚫", "Отменена"),
}

# ЕДИНСТВЕННОЕ место, где описано, какие переходы разрешены (решение заказчика).
# По нему репозиторий отвергает недопустимые смены статуса, а клавиатура карточки
# показывает только допустимые кнопки — правило не может разойтись в двух местах.
ALLOWED_TRANSITIONS: dict[ApplicationStatus, tuple[ApplicationStatus, ...]] = {
    ApplicationStatus.NEW: (ApplicationStatus.IN_WORK, ApplicationStatus.CANCELLED),
    ApplicationStatus.IN_WORK: (
        ApplicationStatus.DONE,
        ApplicationStatus.CANCELLED,
        ApplicationStatus.NEW,  # «Вернуть в новые»
    ),
    ApplicationStatus.DONE: (ApplicationStatus.NEW,),
    ApplicationStatus.CANCELLED: (ApplicationStatus.NEW,),
}


def can_transition(current: ApplicationStatus, target: ApplicationStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


class ApplicationRecord(BaseModel):
    """Заявка, как она лежит в базе: сама заявка + её судьба у администратора."""

    model_config = ConfigDict(frozen=True)

    application: Application
    status: ApplicationStatus
    delivered_at: datetime | None = None
    # Когда взята в работу (NULL у новой) и когда закрыта (done/cancelled).
    taken_at: datetime | None = None
    closed_at: datetime | None = None
    # Когда клиенту сообщили «заявка принята в работу» (None — не сообщали).
    client_notified_at: datetime | None = None
