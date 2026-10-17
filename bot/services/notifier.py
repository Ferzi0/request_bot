"""Отправка заявки администратору с повторами.

Что повторяем: сетевые сбои, 5xx и flood control (RetryAfter) — они временные.
Что не повторяем: 400/403 (например, администратор ещё не нажал /start
у бота или заблокировал его) — повтор не поможет, нужна реакция человека.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from bot.keyboards import admin_application_keyboard
from bot.models import Application
from bot.services.formatting import admin_reply_url, format_admin_application

logger = logging.getLogger(__name__)

# Telegram не даёт кнопку tg://user?id=, если пользователь запретил ссылки
# на свой аккаунт в настройках приватности.
_PRIVACY_RESTRICTED = "BUTTON_USER_PRIVACY_RESTRICTED"
_MAX_RETRY_AFTER = 30.0  # дольше держать клиента в ожидании нет смысла

Sleep = Callable[[float], Awaitable[None]]


class AdminNotifier:
    def __init__(
        self,
        admin_id: int,
        tz: ZoneInfo,
        retry_delays: Sequence[float] = (1.0, 3.0),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._admin_id = admin_id
        self._tz = tz
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep  # подменяется в тестах, чтобы не ждать по-настоящему

    async def notify(self, bot: Bot, app: Application) -> bool:
        """True — заявка доставлена администратору. Исключений наружу не бросает."""
        text = format_admin_application(app, self._tz)
        reply_url: str | None = admin_reply_url(app)
        attempts = len(self._retry_delays) + 1

        attempt = 1
        while attempt <= attempts:
            markup = admin_application_keyboard(app.id, reply_url)
            try:
                await bot.send_message(self._admin_id, text, reply_markup=markup)
            except TelegramBadRequest as exc:
                if (
                    reply_url
                    and reply_url.startswith("tg://")
                    and _PRIVACY_RESTRICTED in exc.message
                ):
                    # Не попытка, а уточнение: шлём без кнопки, ID есть в тексте.
                    logger.info("Заявка %s: кнопка tg://user недоступна, шлю без неё", app.id)
                    reply_url = None
                    continue
                return self._give_up(app, exc)
            except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
                if attempt == attempts:
                    return self._give_up(app, exc)
                delay = self._retry_delays[attempt - 1]
                if isinstance(exc, TelegramRetryAfter):
                    delay = min(float(exc.retry_after), _MAX_RETRY_AFTER)
                logger.warning(
                    "Заявка %s: временная ошибка отправки (%s), попытка %d/%d, повтор через %.1f с",
                    app.id,
                    type(exc).__name__,
                    attempt,
                    attempts,
                    delay,
                )
                await self._sleep(delay)
                attempt += 1
                continue
            except TelegramAPIError as exc:  # 401/403/404 и прочие постоянные
                return self._give_up(app, exc)
            logger.info("Заявка %s доставлена администратору (попытка %d)", app.id, attempt)
            return True
        return False  # сюда не дойдём, но так явно для читателя и mypy

    @staticmethod
    def _give_up(app: Application, exc: TelegramAPIError) -> bool:
        # exc.message — описание от Telegram («chat not found» и т. п.), без данных заявки.
        logger.error(
            "Заявка %s НЕ доставлена администратору: %s: %s",
            app.id,
            type(exc).__name__,
            exc.message,
        )
        return False
