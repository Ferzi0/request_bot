"""Какое сообщение в чате администратора сейчас — «живая» админ-панель.

Нужно, чтобы повторный /admin удалял старую панель и в чате оставалась одна.
Храним в памяти, а не в базе (решение заказчика): после перезапуска бот старую
панель забывает и она остаётся в чате — это принято. Отдельный класс, а не
глобальная переменная в хендлере: его кладут в workflow_data и подменяют в тестах.
"""

from __future__ import annotations


class PanelMessages:
    """chat_id → message_id текущего сообщения панели (на практике один чат — админа)."""

    def __init__(self) -> None:
        self._ids: dict[int, int] = {}

    def get(self, chat_id: int) -> int | None:
        return self._ids.get(chat_id)

    def set(self, chat_id: int, message_id: int) -> None:
        self._ids[chat_id] = message_id
