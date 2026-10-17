"""Доменные модели: заявка и её черновик.

Модели не знают про Telegram-разметку и хендлеры — их можно сохранить
в БД, отправить на почту или в другой канал без изменений.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum

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
        if not (name and contact and project_type and task and draft.consent):
            raise ValueError("draft is incomplete")
        return cls(
            id=draft.app_id or new_application_id(now),
            created_at=now,
            source=draft.source,
            name=name,
            contact=contact,
            project_type=project_type,
            task=task,
            applicant=applicant,
        )


def new_application_id(now: datetime) -> str:
    """Номер заявки без БД: дата + 4 случайных hex-символа, например 261017-3F9A.

    Счётчик в памяти обнулялся бы при перезапуске и давал повторы; случайный
    хвост уникален на практике и не раскрывает, сколько всего заявок.
    """
    return f"{now:%y%m%d}-{secrets.token_hex(2).upper()}"
