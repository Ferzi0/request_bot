"""Сборка HTML-сообщений из шаблонов texts.py.

Единственное место, где пользовательский ввод попадает в разметку, и здесь
он ВСЕГДА проходит через html.escape. Иначе имя вида «<a href=…>» или «</b>»
сломало бы сообщение (Telegram вернул бы ошибку разбора) или подменило ссылку.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from bot import texts
from bot.models import Application, ApplicationDraft, ApplicationRecord
from bot.services.storage import DatabaseStatus
from bot.services.validation import telegram_username_url


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def format_summary(draft: ApplicationDraft) -> str:
    return texts.SUMMARY.format(
        name=_e(draft.name or "—"),
        contact=_e(draft.contact or "—"),
        project_type=_e(draft.project_type.label if draft.project_type else "—"),
        task=_e(draft.task or "—"),
    )


def format_consent(policy_url: str | None) -> str:
    if not policy_url:
        return texts.ASK_CONSENT_NO_POLICY
    # URL из нашего же .env, но в атрибут href всё равно подставляем экранированным.
    return texts.ASK_CONSENT_WITH_POLICY.format(policy_url=_e(policy_url))


def format_send_failed(fallback_contact: str | None) -> str:
    if not fallback_contact:
        return texts.SEND_FAILED_NO_CONTACT
    return texts.SEND_FAILED_WITH_CONTACT.format(contact=_e(fallback_contact))


def format_local_time(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def format_admin_application(
    app: Application, tz: ZoneInfo, heading: str = texts.ADMIN_HEADING_NEW
) -> str:
    applicant = app.applicant
    username = f"@{applicant.username}" if applicant.username else texts.ADMIN_NO_USERNAME
    # Упоминание по id кликабельно, даже если ника нет (если пользователь это разрешает).
    tg_name = f'<a href="tg://user?id={applicant.user_id}">{_e(applicant.full_name)}</a>'
    return texts.ADMIN_APPLICATION.format(
        heading=_e(heading),
        app_id=_e(app.id),
        created_at=_e(format_local_time(app.created_at, tz)),
        source=_e(app.source.label),
        name=_e(app.name),
        contact=_e(app.contact),
        project_type=_e(app.project_type.label),
        task=_e(app.task),
        user_id=applicant.user_id,
        username=_e(username),
        tg_name=tg_name,
    )


def admin_reply_url(app: Application) -> str:
    """Ссылка для кнопки «Написать клиенту».

    Порядок: ник из Telegram → ник, указанный в контакте → tg://user?id=.
    t.me-ссылка работает всегда; tg://user?id= — только если настройки
    приватности пользователя это позволяют (иначе Telegram отклонит кнопку,
    см. notifier).
    """
    if app.applicant.username:
        return f"https://t.me/{app.applicant.username}"
    if url := telegram_username_url(app.contact):
        return url
    return f"tg://user?id={app.applicant.user_id}"


def format_database_status(status: DatabaseStatus) -> str:
    """Строка о базе для стартового сообщения администратору."""
    if status.error is not None:
        return texts.ADMIN_DB_UNAVAILABLE.format(error=escape(status.error))
    return texts.ADMIN_DB_OK.format(version=status.schema_version, count=status.applications)


# ---------- админ-панель ----------


def format_panel_list(
    records: list[ApplicationRecord],
    title: str,
    *,
    total: int,
    page: int,
    pages: int,
    tz: ZoneInfo,
) -> str:
    """Страница списка: номер, дата, имя, тип проекта, статус.

    Контакт и задачу намеренно не показываем: список виден целиком, а
    подробности — только в карточке конкретной заявки.
    """
    if not records:
        return texts.PANEL_LIST_EMPTY.format(title=_e(title))
    header = texts.PANEL_LIST_HEADER.format(
        title=_e(title), total=texts.applications_count(total), page=page + 1, pages=pages
    )
    lines = [
        texts.PANEL_LIST_LINE.format(
            icon=record.status.icon,
            app_id=_e(record.application.id),
            created_at=_e(format_local_time(record.application.created_at, tz)),
            name=_e(record.application.name),
            project_type=_e(record.application.project_type.label),
        )
        for record in records
    ]
    return "\n\n".join([header, *lines])


def format_panel_card(record: ApplicationRecord, tz: ZoneInfo) -> str:
    """Карточка: тот же текст, что в уведомлении о заявке, плюс статус и время."""
    lines = [texts.PANEL_CARD_STATUS.format(icon=record.status.icon, label=_e(record.status.label))]
    if record.taken_at is not None:
        lines.append(texts.PANEL_CARD_TAKEN.format(taken_at=format_local_time(record.taken_at, tz)))
    if record.closed_at is not None:
        lines.append(
            texts.PANEL_CARD_CLOSED.format(closed_at=format_local_time(record.closed_at, tz))
        )
    body = format_admin_application(record.application, tz, heading=texts.ADMIN_HEADING_CARD)
    return body + "\n\n" + "\n".join(lines)


def format_panel_confirm_cancel(record: ApplicationRecord, tz: ZoneInfo) -> str:
    return (
        format_panel_card(record, tz)
        + "\n\n"
        + texts.PANEL_CONFIRM_CANCEL.format(app_id=_e(record.application.id))
    )
