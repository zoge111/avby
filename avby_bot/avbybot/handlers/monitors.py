"""Мои мониторинги: список, карточка, пауза/запуск, удаление, «Стоп все»."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from ..context import App
from ..formatting import esc, monitor_card
from ..keyboards import btn, kb, monitor_kb
from ..utils import safe_edit

router = Router(name="monitors")


def _short(text: str, n: int = 46) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _mid(cb: CallbackQuery) -> int | None:
    try:
        return int(cb.data.split(":")[2])
    except (IndexError, ValueError):
        return None


async def show_list(cb: CallbackQuery, app: App) -> None:
    rows = await app.db.list_monitors(cb.from_user.id)
    if not rows:
        await safe_edit(
            cb.message, "📋 У вас пока нет мониторингов.",
            kb([btn("➕ Добавить мониторинг", "menu:add")], [btn("🏠 В меню", "menu:home")]),
        )
        return
    buttons = [
        [btn(f"{'🟢' if r['is_active'] else '⏸'} {_short(r['title'])}", f"mon:open:{r['id']}")] for r in rows[:40]
    ]
    buttons.append([btn("🏠 В меню", "menu:home")])
    await safe_edit(
        cb.message,
        f"📋 <b>Мои мониторинги</b> ({len(rows)})\n🟢 работает · ⏸ на паузе\n\nВыберите мониторинг:",
        kb(*buttons),
    )


@router.callback_query(F.data == "menu:list")
async def my_list(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    await cb.answer()
    await show_list(cb, app)


@router.callback_query(F.data.startswith("mon:open:"))
async def open_card(cb: CallbackQuery, app: App) -> None:
    row = await app.db.get_monitor(cb.from_user.id, _mid(cb) or 0)
    if not row:
        await cb.answer("Мониторинг не найден", show_alert=True)
        return
    await cb.answer()
    await safe_edit(cb.message, monitor_card(row), monitor_kb(row))


@router.callback_query(F.data.startswith("mon:toggle:"))
async def toggle(cb: CallbackQuery, app: App) -> None:
    uid, mid = cb.from_user.id, _mid(cb) or 0
    row = await app.db.get_monitor(uid, mid)
    if not row:
        await cb.answer("Мониторинг не найден", show_alert=True)
        return
    if not row["is_active"]:
        ok, limit = await app.check_limit(uid)
        if not ok:
            await cb.answer(f"Лимит бесплатного тарифа — {limit} активных мониторинга. Нужен Premium.", show_alert=True)
            return
    await app.db.set_active(uid, mid, not row["is_active"])
    row = await app.db.get_monitor(uid, mid)
    await cb.answer("Запущен ▶️" if row["is_active"] else "Поставлен на паузу ⏸")
    await safe_edit(cb.message, monitor_card(row), monitor_kb(row))


@router.callback_query(F.data.startswith("mon:del:"))
async def delete_ask(cb: CallbackQuery, app: App) -> None:
    mid = _mid(cb) or 0
    row = await app.db.get_monitor(cb.from_user.id, mid)
    if not row:
        await cb.answer("Мониторинг не найден", show_alert=True)
        return
    await cb.answer()
    await safe_edit(
        cb.message, f"🗑 Удалить мониторинг <b>{esc(row['title'])}</b>?\nНастройки будут потеряны.",
        kb([btn("✅ Да, удалить", f"mon:delyes:{mid}"), btn("↩️ Нет", f"mon:open:{mid}")]),
    )


@router.callback_query(F.data.startswith("mon:delyes:"))
async def delete_do(cb: CallbackQuery, app: App) -> None:
    await app.db.delete_monitor(cb.from_user.id, _mid(cb) or 0)
    await cb.answer("Удалено 🗑")
    await show_list(cb, app)


@router.callback_query(F.data == "menu:stop")
async def stop_all(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    n = await app.db.stop_all(cb.from_user.id)
    await cb.answer()
    text = (
        f"⏹ Остановлено мониторингов: <b>{n}</b>.\nЗапустить снова можно в «📋 Мои мониторинги»."
        if n else "Активных мониторингов нет."
    )
    await safe_edit(cb.message, text, kb([btn("📋 Мои мониторинги", "menu:list")], [btn("🏠 В меню", "menu:home")]))

