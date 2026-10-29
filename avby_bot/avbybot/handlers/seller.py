"""Отслеживание продавца / автохауса: ссылка на любое его объявление или название."""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..avby import AvByError
from ..context import App
from ..formatting import FIRST_PASS_NOTE, filter_title, monitor_card
from ..keyboards import btn, kb, monitor_kb
from ..utils import safe_edit

log = logging.getLogger(__name__)
router = Router(name="seller")

TEXT = F.text & ~F.text.startswith("/")
_URL = re.compile(r"(?i)^(https?://)?([\w-]+\.)*av\.by(/|$)")


class S(StatesGroup):
    waiting = State()


@router.callback_query(F.data == "menu:seller")
async def seller_start(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await cb.answer()
    ok, limit = await app.check_limit(cb.from_user.id)
    if not ok:
        await state.clear()
        await safe_edit(
            cb.message, app.limit_text(limit),
            kb([btn("📋 Мои мониторинги", "menu:list")], [btn("🏠 В меню", "menu:home")]),
        )
        return
    await state.clear()
    await state.set_state(S.waiting)
    await safe_edit(
        cb.message,
        "👤 <b>Отслеживание продавца / автохауса</b>\n\n"
        "Пришлите <b>ссылку на любое объявление</b> этого продавца на av.by "
        "или <b>название автохауса</b> текстом.\n"
        "Я буду присылать каждое его новое объявление.",
        kb([btn("✖️ Отмена", "w:cancel")]),
    )


@router.message(StateFilter(S.waiting), TEXT)
async def seller_got(message: Message, state: FSMContext, app: App) -> None:
    text = message.text.strip()
    seller_id = seller_name = ""
    if _URL.match(text):
        try:
            found = await app.client.fetch_seller(text)
        except AvByError as e:
            log.warning("Продавец по ссылке не определён: %s", e)
            found = None
        if not found:
            await message.answer(
                "Не удалось определить продавца по ссылке 😕\n"
                "Пришлите ссылку на конкретное объявление или напишите название продавца текстом.",
                reply_markup=kb([btn("✖️ Отмена", "w:cancel")]),
            )
            return
        seller_id, seller_name = found
    else:
        seller_name = text[:80]

    ok, limit = await app.check_limit(message.chat.id)
    await state.clear()
    if not ok:
        await message.answer(app.limit_text(limit), reply_markup=kb([btn("🏠 В меню", "menu:home")]))
        return
    f = {"seller_id": seller_id or None, "seller_name": seller_name or None}
    mid = await app.db.add_monitor(message.chat.id, "seller", filter_title(f), f)
    row = await app.db.get_monitor(message.chat.id, mid)
    await message.answer(
        "✅ <b>Слежу за продавцом!</b>\n\n" + monitor_card(row) + "\n\n" + FIRST_PASS_NOTE,
        reply_markup=monitor_kb(row),
    )
