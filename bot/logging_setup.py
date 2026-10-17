"""Настройка стандартного logging.

Правило проекта: в логах только служебные данные (id, длины, этапы),
без текста заявок и без токена.
"""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RedactingFormatter(logging.Formatter):
    """Вырезает секреты из итоговой строки лога, включая трейсбеки.

    Страховка «на всякий случай»: даже если какая-то библиотека напечатает
    URL вида .../bot<токен>/getMe, токен в журнал не попадёт.
    """

    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(_FORMAT, secrets or []))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # aiohttp на DEBUG слишком многословен и бесполезен для этого бота.
    logging.getLogger("aiohttp").setLevel(max(logging.INFO, root.level))
