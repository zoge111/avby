"""Последний роутер: всё, что не поймали остальные (устаревшие кнопки, случайный текст)."""
from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from ..keyboards import main_menu

router = Router(name="fallback")


@router.message()
async def unknown_message(message: Message) -> None:
    await message.answer("Не понял 🤔 Откройте меню кнопкой ниже или командой /start.", reply_markup=main_menu())


@router.callback_query()
async def stale_button(cb: CallbackQuery) -> None:
    await cb.answer("Эта кнопка устарела. Откройте меню: /start", show_alert=True)
