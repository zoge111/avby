"""/start, проверка обязательной подписки, главное меню."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..context import App
from ..formatting import WELCOME
from ..keyboards import main_menu
from ..utils import safe_edit

router = Router(name="start")


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot, app: App) -> None:
    await state.clear()
    u = message.from_user
    await app.db.upsert_user(u.id, u.username, u.first_name)
    if not await app.gate.check(bot, u.id, fresh=True):
        await app.gate.send_gate(message)
        return
    await message.answer(WELCOME, reply_markup=main_menu())


@router.callback_query(F.data == "sub:check")
async def sub_check(cb: CallbackQuery, bot: Bot, app: App) -> None:
    if not await app.gate.check(bot, cb.from_user.id, fresh=True):
        await cb.answer("Подписка не найдена. Подпишитесь на канал и нажмите кнопку ещё раз.", show_alert=True)
        return
    u = cb.from_user
    await app.db.upsert_user(u.id, u.username, u.first_name)
    await cb.answer("Спасибо! ✅")
    await safe_edit(cb.message, WELCOME, main_menu())


@router.callback_query(F.data == "menu:home")
async def home(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await safe_edit(cb.message, WELCOME, main_menu())
