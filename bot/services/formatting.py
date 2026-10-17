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
from bot.models import Application, ApplicationDraft
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


def format_admin_application(app: Application, tz: ZoneInfo) -> str:
    applicant = app.applicant
    username = f"@{applicant.username}" if applicant.username else texts.ADMIN_NO_USERNAME
    # Упоминание по id кликабельно, даже если ника нет (если пользователь это разрешает).
    tg_name = f'<a href="tg://user?id={applicant.user_id}">{_e(applicant.full_name)}</a>'
    return texts.ADMIN_APPLICATION.format(
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
