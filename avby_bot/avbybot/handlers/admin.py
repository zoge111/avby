"""Админ-панель: /admin, /stats, /broadcast, /premium."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..context import App
from ..formatting import MINSK_TZ
from ..keyboards import btn, kb
from ..utils import safe_edit

log = logging.getLogger(__name__)
router = Router(name="admin")
_tasks: set[asyncio.Task] = set()  # ссылки на фоновые рассылки, чтобы их не собрал GC


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, app: App) -> bool:
        return event.from_user is not None and event.from_user.id in app.cfg.admin_ids


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class Bc(StatesGroup):
    waiting = State()
    confirm = State()


ADMIN_HELP = (
    "🛠 <b>Админ-панель</b>\n\n"
    "/stats — статистика\n"
    "/broadcast — рассылка всем пользователям (текст, фото, видео…)\n"
    "/premium <code>&lt;user_id&gt; &lt;дней&gt;</code> — выдать/продлить Premium (0 дней — снять)"
)


async def stats_text(app: App) -> str:
    s = await app.db.stats()
    c = s["counters"]
    last = c.get("last_cycle_ts")
    last_s = datetime.fromtimestamp(last, MINSK_TZ).strftime("%d.%m %H:%M:%S") if last else "ещё не было"
    return (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{s['users']}</b> (заблокировали бота: {s['blocked']})\n"
        f"💎 Premium: <b>{s['premium']}</b>\n"
        f"🔎 Мониторингов: <b>{s['monitors']}</b> (активных: {s['active']})\n"
        f"🌐 Запросов к парсеру: <b>{c.get('parser_requests', 0)}</b> (ошибок: {c.get('parser_errors', 0)})\n"
        f"🆕 Объявлений обработано: <b>{c.get('adverts_seen', 0)}</b>\n"
        f"📨 Уведомлений отправлено: <b>{c.get('alerts_sent', 0)}</b>\n"
        f"⏱ Последний успешный цикл парсера: {last_s}\n"
        f"🛰 Источник данных: {app.client.source or 'ещё не определён'}"
    )


@router.message(Command("admin"))
async def admin_home(message: Message) -> None:
    await message.answer(ADMIN_HELP, reply_markup=kb([btn("📊 Статистика", "adm:stats")]))


@router.message(Command("stats"))
async def cmd_stats(message: Message, app: App) -> None:
    await message.answer(await stats_text(app))


@router.callback_query(F.data == "adm:stats")
async def cb_stats(cb: CallbackQuery, app: App) -> None:
    await cb.answer()
    await safe_edit(cb.message, await stats_text(app), kb([btn("🔄 Обновить", "adm:stats")]))


# --- Premium ---------------------------------------------------------------------------

@router.message(Command("premium"))
async def cmd_premium(message: Message, command: CommandObject, bot: Bot, app: App) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer("Использование: <code>/premium &lt;user_id&gt; &lt;дней&gt;</code>\n0 дней — снять Premium.")
        return
    uid, days = int(args[0]), int(args[1])
    user = await app.db.get_user(uid)
    if not user:
        await message.answer("Пользователь не найден: он должен хотя бы раз нажать /start.")
        return
    now = int(time.time())
    until = 0 if days == 0 else max(now, user["premium_until"]) + days * 86400
    await app.db.set_premium(uid, until)
    if days == 0:
        await message.answer(f"Premium снят у {uid}.")
        return
    date = datetime.fromtimestamp(until, MINSK_TZ).strftime("%d.%m.%Y")
    await message.answer(f"💎 Premium для {uid} до {date}.")
    try:
        await bot.send_message(uid, f"💎 Вам включён <b>Premium</b> до {date}. Лимитов на мониторинги больше нет!")
    except TelegramAPIError:
        pass


# --- Рассылка --------------------------------------------------------------------------

@router.message(Command("broadcast"))
async def bc_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Bc.waiting)
    await message.answer("📣 Пришлите сообщение для рассылки (текст, фото, видео, документ…).\n/cancel — отмена.")


@router.message(Command("cancel"), StateFilter(Bc.waiting, Bc.confirm))
async def bc_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Рассылка отменена.")


@router.message(StateFilter(Bc.waiting))
async def bc_got(message: Message, state: FSMContext, app: App) -> None:
    await state.update_data(chat_id=message.chat.id, mid=message.message_id)
    await state.set_state(Bc.confirm)
    total = len(await app.db.all_user_ids())
    await message.answer(
        f"Отправить это сообщение <b>{total}</b> пользователям?",
        reply_markup=kb([btn("✅ Отправить", "bc:go"), btn("✖️ Отмена", "bc:no")]),
    )


@router.callback_query(StateFilter(Bc.confirm), F.data == "bc:no")
async def bc_no(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer()
    await safe_edit(cb.message, "Рассылка отменена.")


@router.callback_query(StateFilter(Bc.confirm), F.data == "bc:go")
async def bc_go(cb: CallbackQuery, state: FSMContext, bot: Bot, app: App) -> None:
    data = await state.get_data()
    await state.clear()
    await cb.answer()
    await safe_edit(cb.message, "📣 Рассылка запущена. Пришлю отчёт, когда закончу.")
    task = asyncio.create_task(run_broadcast(bot, app, data["chat_id"], data["mid"], cb.from_user.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def run_broadcast(bot: Bot, app: App, src_chat: int, src_mid: int, admin_id: int) -> None:
    ids = await app.db.all_user_ids()
    ok = blocked = failed = 0
    for uid in ids:
        for attempt in range(2):
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=src_chat, message_id=src_mid)
                ok += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                if attempt == 0:
                    continue
                failed += 1
            except TelegramForbiddenError:
                blocked += 1
                await app.db.mark_blocked(uid)
            except TelegramAPIError:
                failed += 1
            break
        await asyncio.sleep(0.05)  # ~20 сообщений/сек
    log.info("Рассылка: доставлено %s, заблокировали %s, ошибок %s", ok, blocked, failed)
    try:
        await bot.send_message(
            admin_id, f"📣 <b>Рассылка завершена</b>\nДоставлено: {ok}\nЗаблокировали бота: {blocked}\nОшибок: {failed}"
        )
    except TelegramAPIError:
        pass
