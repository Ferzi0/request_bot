"""Черновик заявки в хранилище FSM.

FSM хранит dict; здесь он превращается в типизированную модель и обратно,
чтобы хендлеры не работали с «магическими» ключами словаря.
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext

from bot.models import ApplicationDraft, Source


async def load_draft(state: FSMContext) -> ApplicationDraft:
    return ApplicationDraft.model_validate(await state.get_data())


async def save_draft(state: FSMContext, draft: ApplicationDraft) -> None:
    await state.set_data(draft.model_dump(mode="json"))


async def reset_form(state: FSMContext, source: Source | None = None) -> ApplicationDraft:
    """Сбрасывает шаг и ответы, но помнит, откуда пришёл человек (сайт/бот)."""
    if source is None:
        source = (await load_draft(state)).source
    await state.clear()
    draft = ApplicationDraft(source=source)
    await save_draft(state, draft)
    return draft
