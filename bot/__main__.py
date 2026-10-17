"""Точка входа: `python -m bot` — запуск long polling."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)

from bot.app import create_bot, create_dispatcher
from bot.config import ConfigError, Settings, load_settings
from bot.logging_setup import setup_logging

logger = logging.getLogger("bot")


async def run(settings: Settings) -> int:
    bot = create_bot(settings)
    dp = create_dispatcher(settings)
    try:
        # Проверяем токен заранее: так ошибка понятна сразу, а не утонет в ретраях polling.
        try:
            me = await bot.get_me()
            # Если когда-то был включён webhook, polling с ним конфликтует. Накопившиеся
            # апдейты НЕ сбрасываем: это могут быть заявки, пришедшие, пока бот был выключен.
            await bot.delete_webhook(drop_pending_updates=False)
        except TelegramUnauthorizedError:
            logger.error(
                "Telegram отклонил токен (Unauthorized): BOT_TOKEN недействителен. "
                "Скопируйте актуальный токен из @BotFather в .env."
            )
            return 1
        except TelegramNetworkError as exc:
            logger.error(
                "Нет связи с Telegram (%s). Проверьте интернет и запустите снова.", exc.message
            )
            return 1
        except TelegramAPIError as exc:  # 5xx, flood control и прочее — без трейсбека
            logger.error(
                "Telegram ответил ошибкой (%s: %s). Попробуйте запустить позже.",
                type(exc).__name__,
                exc.message,
            )
            return 1

        logger.info("Бот @%s запущен (long polling). Остановка — Ctrl+C.", me.username)
        # start_polling сам ловит SIGINT/SIGTERM и завершает обработку аккуратно.
        await dp.start_polling(bot, close_bot_session=False)
        return 0
    finally:
        await bot.session.close()
        logger.info("Бот остановлен.")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Ошибка настройки:\n{exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level, secrets=[settings.bot_token.get_secret_value()])
    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:  # Ctrl+C до старта polling
        return 130


if __name__ == "__main__":
    sys.exit(main())
