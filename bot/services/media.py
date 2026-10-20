"""Картинки для клиента: приветствие и «Заявка принята».

Картинка — украшение, а не суть: если её не удалось отправить (нет файла, Telegram
отказал, сбой сети, слишком длинная подпись), тот же текст с теми же кнопками уходит
обычным сообщением. Клиент не должен остаться без приветствия или подтверждения.

Файл загружается в Telegram один раз; дальше отправляем по file_id (кэш в памяти).
После перезапуска кэш пуст — файл загрузится снова, это нормально.
Администратору картинки не отправляются (решение заказчика).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile, InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

# Лимит подписи к фото в Telegram — 1024 символа (UTF-16) после разбора HTML.
# Считаем по исходному HTML: он не короче видимого текста, значит, оценка с запасом.
CAPTION_LIMIT = 1024


class Picture(StrEnum):
    """Картинка = имя файла в bot/assets (собираются tools/images/build.sh)."""

    WELCOME = "welcome.jpg"
    ACCEPTED = "accepted.jpg"


def fits_caption(caption: str) -> bool:
    return len(caption.encode("utf-16-le")) // 2 <= CAPTION_LIMIT


class PhotoSender:
    def __init__(self, assets_dir: Path = ASSETS_DIR) -> None:
        self._assets_dir = assets_dir
        self._file_ids: dict[Picture, str] = {}

    async def send(
        self,
        bot: Bot,
        chat_id: int,
        picture: Picture,
        caption: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        """Фото с подписью, а при любой неудаче — тот же текст обычным сообщением.

        Ошибка запасного текстового сообщения (например, клиент заблокировал бота)
        уходит наружу, как и раньше, когда картинок не было.
        """
        try:
            return await self._send_photo(bot, chat_id, picture, caption, reply_markup)
        except Exception as exc:
            # В лог — только картинка и класс ошибки: подпись может содержать номер и имя.
            logger.warning(
                "Картинка %s не отправлена (%s) — отправляю текстом",
                picture.value,
                type(exc).__name__,
            )
        return await bot.send_message(chat_id, caption, reply_markup=reply_markup)

    async def _send_photo(
        self,
        bot: Bot,
        chat_id: int,
        picture: Picture,
        caption: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> Message:
        if not fits_caption(caption):
            raise ValueError("caption is too long for a photo")
        cached = self._file_ids.get(picture)
        if cached is None:
            path = self._assets_dir / picture.value
            if not path.is_file():
                raise FileNotFoundError(picture.value)
            photo: str | FSInputFile = FSInputFile(path)
        else:
            photo = cached
        try:
            sent = await bot.send_photo(chat_id, photo, caption=caption, reply_markup=reply_markup)
        except Exception:
            if cached is not None and self._file_ids.get(picture) == cached:
                # file_id мог устареть — в следующий раз загрузим файл заново.
                # Сравниваем: пока шёл запрос, другой клиент мог уже загрузить
                # файл и положить свежий file_id — его не трогаем.
                del self._file_ids[picture]
            raise
        if sent.photo:
            # Самый крупный размер — последний; его file_id отправит то же фото без загрузки.
            self._file_ids[picture] = sent.photo[-1].file_id
        return sent
