"""Роутеры бота.

Роутеры создаются фабриками, а не живут в модулях как глобальные объекты:
aiogram разрешает подключить роутер только к одному родителю, а тестам
нужен свежий диспетчер на каждый тест.
"""

from aiogram import Router

from bot.handlers import admin, application, common, errors, start


def get_routers() -> list[Router]:
    # Порядок важен: апдейт обрабатывает первый подошедший хендлер.
    return [
        errors.create_router(),  # исключения из любых хендлеров
        start.create_router(),  # /start сбрасывает любой шаг
        common.create_commands_router(),  # /help, /cancel и «Отмена» — на любом шаге
        admin.create_router(),  # кнопки под заявками у администратора
        application.create_router(),  # шаги заявки
        common.create_fallback_router(),  # всё остальное — последним
    ]
