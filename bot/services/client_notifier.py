"""Сообщение клиенту «заявка принята в работу».

Одно место для обоих путей: кнопки «✅ В работу» под уведомлением и панели /admin.
Пишем только при переходе new → in_work и только один раз за жизнь заявки:
перед отправкой атомарно ставится метка client_notified_at (repository.claim_…).

Что с меткой, если отправка не удалась:
- клиент заблокировал бота, удалил аккаунт, чат не найден (403/400) — повтор не
  поможет: метка остаётся, администратору — короткое уведомление;
- сеть, 5xx, flood control — сбой временный: метку снимаем, чтобы следующий переход
  в работу («Вернуть в новые» → «В работу») мог отправить снова; администратору —
  уведомление. Редкий дубль возможен, только если Telegram доставил, но ответ
  потерялся, и администратор сам повторил переход;
- любая другая ошибка — неизвестно, дошло ли: метку оставляем (без дублей), администратору
  — уведомление. Ничего не теряется молча.

Ошибки наружу не выходят: смена статуса и ответ администратору уже состоялись.
В лог и администратору — только номер заявки и класс ошибки.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError

from bot import texts
from bot.services.storage import ApplicationRepository

logger = logging.getLogger(__name__)

# Временные сбои: повторить позже имеет смысл.
_TEMPORARY = (TelegramNetworkError, TelegramServerError, TelegramRetryAfter)


class ClientNotice(StrEnum):
    SENT = "sent"
    SKIPPED = "skipped"  # уже сообщали / заявки нет / не в работе / базы нет
    FAILED = "failed"  # не доставлено — администратору ушло уведомление


def format_client_taken(app_id: str) -> str:
    return texts.CLIENT_TAKEN.format(app_id=escape(app_id))


class ClientNotifier:
    def __init__(self, admin_id: int, clock: Callable[[], datetime] | None = None) -> None:
        self._admin_id = admin_id
        self._clock = clock or (lambda: datetime.now(UTC))  # подменяется в тестах

    async def application_taken(
        self, bot: Bot, repository: ApplicationRepository, app_id: str
    ) -> ClientNotice:
        """Вызывать ПОСЛЕ успешного перехода new → in_work. Исключений не бросает."""
        claimed_at = self._clock()
        try:
            user_id = await repository.claim_client_notice(app_id, claimed_at)
        except Exception as exc:
            error = type(exc).__name__
            logger.error("Заявка %r: метка уведомления клиента не записана: %s", app_id, error)
            await self._tell_admin(bot, app_id, error)
            return ClientNotice.FAILED
        if user_id is None:
            return ClientNotice.SKIPPED  # метку уже поставили раньше — второй раз не пишем

        try:
            await bot.send_message(user_id, format_client_taken(app_id))
        except Exception as exc:
            error = type(exc).__name__
            if isinstance(exc, _TEMPORARY):
                await self._release(repository, app_id, claimed_at)
            logger.warning("Заявка %r: клиенту не доставлено «в работе»: %s", app_id, error)
            await self._tell_admin(bot, app_id, error)
            return ClientNotice.FAILED
        logger.info("Заявка %r: клиенту отправлено «принята в работу»", app_id)
        return ClientNotice.SENT

    async def _release(
        self, repository: ApplicationRepository, app_id: str, claimed_at: datetime
    ) -> None:
        try:
            await repository.release_client_notice(app_id, claimed_at)
        except Exception as exc:
            # Метка осталась — повтора не будет, но и дубля тоже; администратор уведомлён.
            logger.error("Заявка %r: метку уведомления не снять: %s", app_id, type(exc).__name__)

    async def _tell_admin(self, bot: Bot, app_id: str, error: str) -> None:
        # Без троттлинга: каждое такое сообщение — про другого клиента, и появляется
        # оно только в ответ на нажатие администратора, лавины быть не может.
        text = texts.ADMIN_CLIENT_NOTICE_FAILED.format(app_id=escape(app_id), error=escape(error))
        try:
            await bot.send_message(self._admin_id, text)
        except Exception as exc:
            logger.error(
                "Заявка %r: не удалось сообщить администратору о сбое: %s",
                app_id,
                type(exc).__name__,
            )
