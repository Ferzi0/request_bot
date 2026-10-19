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
from bot.services.sqlite_repository import SqliteRepository
from bot.services.storage import ApplicationRepository, DatabaseStatus, NullRepository

logger = logging.getLogger("bot")


async def open_repository(
    settings: Settings,
) -> tuple[SqliteRepository | None, DatabaseStatus]:
    """Открывает базу заявок; при неудаче бот работает без неё (только пересылка).

    Доставка администратору важнее архива: из-за испорченного файла или прав
    доступа бот не должен перестать принимать заявки. Состояние базы уходит
    администратору в стартовом сообщении (см. on_startup в bot/app.py).
    """
    repository = SqliteRepository(settings.database_path)
    try:
        await repository.open()
        status = await repository.status()
    except Exception as exc:
        await repository.close()  # если open прошёл, а status нет — не оставляем поток
        logger.error(
            "База заявок не открылась (%s: %s), путь DATABASE_PATH: %s. "
            "Бот работает БЕЗ сохранения заявок в базу — только пересылка в Telegram.",
            type(exc).__name__,
            exc,
            settings.database_path,
        )
        return None, DatabaseStatus(error=type(exc).__name__)
    logger.info(
        "База заявок: %s (схема v%d, заявок: %d)",
        settings.database_path,
        status.schema_version,
        status.applications,
    )
    return repository, status


async def run(settings: Settings) -> int:
    sqlite, database = await open_repository(settings)
    repository: ApplicationRepository = sqlite or NullRepository()
    bot = create_bot(settings)
    dp = create_dispatcher(settings, repository=repository, database=database)
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
        if sqlite is not None:
            await sqlite.close()
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
