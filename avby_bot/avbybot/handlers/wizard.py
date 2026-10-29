"""Пошаговый мастер (FSM): «➕ Добавить мониторинг» и «🚗 Поиск по параметрам».

Шаги: марка → модель → регион → год от/до → валюта → цена от/до → топливо → КПП → кузов → минус-слова.
В режиме «поиск» шагов меньше (марка, модель, год, валюта, цена) и вместо сохранения показываются
результаты с кнопкой «🔔 Следить за этим поиском».

Навигация: на каждом шаге есть «◀️ Назад» и «✖️ Отмена»; необязательные шаги можно пропустить («-» или кнопка).
Ответ пользователя сворачивает вопрос в строку «Марка: Audi», так чат остаётся чистым.
"""
from __future__ import annotations

import copy
import difflib
import logging
import re
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..avby import AvByError, search_adverts
from ..context import App
from ..formatting import FIRST_PASS_NOTE, WELCOME, esc, filter_title, monitor_card
from ..keyboards import btn, chunk, kb, main_menu, monitor_kb, to_menu
from ..models import BODIES, FUELS, GEARBOXES, REGIONS, norm
from ..notify import send_advert
from ..utils import safe_edit

log = logging.getLogger(__name__)
router = Router(name="wizard")

TEXT = F.text & ~F.text.startswith("/")
PAGE = 20  # кнопок марок/моделей на странице


class W(StatesGroup):
    brand = State()
    model = State()
    region = State()
    year_from = State()
    year_to = State()
    currency = State()
    price_from = State()
    price_to = State()
    fuel = State()
    gearbox = State()
    body = State()
    stopwords = State()


FULL = ["brand", "model", "region", "year_from", "year_to", "currency", "price_from", "price_to",
        "fuel", "gearbox", "body", "stopwords"]
QUICK = ["brand", "model", "year_from", "year_to", "currency", "price_from", "price_to"]
LABEL = {
    "brand": "Марка", "model": "Модель", "region": "Регион", "year_from": "Год от", "year_to": "Год до",
    "currency": "Валюта", "price_from": "Цена от", "price_to": "Цена до", "fuel": "Топливо",
    "gearbox": "КПП", "body": "Кузов", "stopwords": "Минус-слова",
}
NUMERIC = ("year_from", "year_to", "price_from", "price_to")
SKIPPABLE = NUMERIC + ("stopwords",)
MULTI = {
    "fuel": ("⛽ Тип топлива", FUELS),
    "gearbox": ("⚙️ Коробка передач", GEARBOXES),
    "body": ("🚙 Тип кузова", BODIES),
}
DEFAULT_F: dict[str, Any] = {
    "brand": None, "model": None, "region": None, "year_from": None, "year_to": None,
    "currency": "USD", "price_from": None, "price_to": None,
    "fuel": [], "gearbox": [], "body": [], "stopwords": [],
}


# --- вспомогательное ---------------------------------------------------------------------

def steps_for(data: dict) -> list[str]:
    return QUICK if data.get("mode") == "search" else FULL


async def cur_step(state: FSMContext) -> str | None:
    s = await state.get_state()
    for name in FULL:
        if getattr(W, name).state == s:
            return name
    return None


def nav_rows(step: str, steps: list[str], skip: bool = False) -> list[list]:
    rows = [[btn("⏭ Пропустить", "w:skip")]] if skip else []
    row = [btn("◀️ Назад", "w:back")] if steps.index(step) > 0 else []
    row.append(btn("✖️ Отмена", "w:cancel"))
    rows.append(row)
    return rows


def pick_names(query: str, names: list[str]) -> list[str]:
    """Подбор марки/модели по введённому тексту: точное, по началу, по вхождению, «похоже»."""
    q = norm(query)
    if not q:
        return []
    exact = [n for n in names if norm(n) == q]
    if exact:
        return exact[:1]
    prefix = [n for n in names if norm(n).startswith(q)]
    inside = [n for n in names if q in norm(n) and n not in prefix]
    res = prefix + inside
    if not res:
        by_key = {norm(n): n for n in names}
        res = [by_key[k] for k in difflib.get_close_matches(q, list(by_key), n=5, cutoff=0.6)]
    return res[:8]


def validate_number(step: str, val: int, f: dict) -> str | None:
    if step.startswith("year"):
        top = datetime.now().year + 1
        if not 1950 <= val <= top:
            return f"Год должен быть в диапазоне 1950–{top}."
        if step == "year_to" and f.get("year_from") and val < f["year_from"]:
            return "Максимальный год не может быть меньше минимального."
    else:
        if not 0 < val <= 10**9:
            return "Цена должна быть положительным числом."
        if step == "price_to" and f.get("price_from") and val < f["price_from"]:
            return "Максимальная цена не может быть меньше минимальной."
    return None


def clean_filters(f: dict) -> dict:
    out = {k: v for k, v in f.items() if not k.startswith("_")}
    for k in ("fuel", "gearbox", "body", "stopwords"):
        out[k] = list(out.get(k) or [])
    return out


async def collapse(bot: Bot, chat_id: int, state: FSMContext, text: str) -> None:
    """Заменяет текущий вопрос короткой строкой с ответом и убирает его кнопки."""
    pid = (await state.get_data()).get("pid")
    if not pid:
        return
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=pid)
    except TelegramBadRequest:
        pass


async def expired(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer("Сессия устарела — начните заново.", show_alert=True)
    await safe_edit(cb.message, WELCOME, main_menu())


# --- построение вопросов -----------------------------------------------------------------

async def build_pick(step: str, f: dict, head: str, nav: list[list], state: FSMContext, app: App, page: int):
    if step == "brand":
        names = [i.name for i in await app.catalog.brands()]
        text = head + "🚗 Выберите <b>марку</b> кнопкой или напишите название (например: <i>Audi, BMW, Volkswagen</i>)."
    else:
        names = [i.name for i in await app.catalog.models(f["brand"])]
        text = (
            head + f"🚗 Марка: <b>{esc(f['brand'])}</b>\n"
            "Выберите <b>модель</b> кнопкой, напишите название или нажмите «Все модели»."
        )
        if not names:
            text += "\n<i>Список моделей недоступен — просто напишите модель текстом.</i>"
    await state.update_data(names=names)  # кнопки ссылаются на индексы в этом списке
    pages = max(1, -(-len(names) // PAGE))
    page = max(0, min(page, pages - 1))
    start = page * PAGE
    rows = chunk([btn(n, f"w:pick:{start + i}") for i, n in enumerate(names[start:start + PAGE])], 2)
    pager = []
    if page > 0:
        pager.append(btn("⬅️", f"w:page:{page - 1}"))
    if pages > 1:
        pager.append(btn(f"{page + 1}/{pages}", "w:noop"))
    if page < pages - 1:
        pager.append(btn("➡️", f"w:page:{page + 1}"))
    if pager:
        rows.append(pager)
    if step == "model":
        rows.append([btn("🚗 Все модели", "w:all")])
    return text, rows + nav


async def build(step: str, data: dict, state: FSMContext, app: App, page: int = 0):
    """(текст, строки кнопок) для шага."""
    f, steps = data["f"], steps_for(data)
    head = f"<b>Шаг {steps.index(step) + 1}/{len(steps)}.</b> "
    if step in ("brand", "model"):
        return await build_pick(step, f, head, nav_rows(step, steps), state, app, page)
    if step == "region":
        rows = chunk([btn(label, f"w:rg:{key}") for key, label in REGIONS.items()], 2)
        rows.append([btn("🇧🇾 Вся Беларусь", "w:rg:all")])
        return head + "📍 Выберите <b>регион</b> поиска:", rows + nav_rows(step, steps)
    if step in ("year_from", "year_to"):
        what, example = ("минимальный", "2015") if step == "year_from" else ("максимальный", "2024")
        return (
            head + f"📅 Введите <b>{what} год</b> выпуска (например, <code>{example}</code>) "
            "или «-», чтобы пропустить.",
            nav_rows(step, steps, skip=True),
        )
    if step == "currency":
        row = [btn(("✅ " if f["currency"] == c else "") + c, f"w:cur:{c}") for c in ("USD", "BYN")]
        return head + "💱 В какой <b>валюте</b> указывать цену?", [row] + nav_rows(step, steps)
    if step in ("price_from", "price_to"):
        what = "минимальную" if step == "price_from" else "максимальную"
        return (
            head + f"💰 Введите <b>{what} цену</b> в {f['currency']} (число) или «-», чтобы пропустить.",
            nav_rows(step, steps, skip=True),
        )
    if step in MULTI:
        title, options = MULTI[step]
        chosen = set(f[step])
        items = [btn(("✅ " if code in chosen else "") + label, f"w:tg:{code}") for code, label in options.items()]
        rows = chunk(items, 2) + [[btn("Готово ➡️", "w:done")]]
        return (
            head + f"{title} — отметьте нужное (можно несколько) и нажмите «Готово». "
            "Ничего не отмечайте, чтобы пропустить.",
            rows + nav_rows(step, steps),
        )
    return (  # stopwords
        head + "🚫 <b>Минус-слова</b> через запятую (например: <i>битый, после ДТП, под разбор</i>) или «-».\n"
        "Объявления с этими словами в заголовке или описании пропускаются. "
        "Можно писать основу слова: «бит» найдёт «битый», «битая».",
        nav_rows(step, steps, skip=True),
    )


async def ask(msg: Message, state: FSMContext, app: App, step: str, *, edit: bool = False) -> None:
    await state.set_state(getattr(W, step))
    data = await state.get_data()
    text, rows = await build(step, data, state, app)
    markup = kb(*rows)
    if edit:
        await safe_edit(msg, text, markup)
        pid = msg.message_id
    else:
        pid = (await msg.answer(text, reply_markup=markup)).message_id
    await state.update_data(pid=pid)


async def advance(msg: Message, state: FSMContext, bot: Bot, app: App) -> None:
    data = await state.get_data()
    steps = steps_for(data)
    i = steps.index(await cur_step(state))  # type: ignore[arg-type]
    if i + 1 < len(steps):
        await ask(msg, state, app, steps[i + 1])
    else:
        await finish(msg, state, bot, app)


# --- завершение ----------------------------------------------------------------------------

async def create_monitor_from(msg: Message, state: FSMContext, app: App, f: dict) -> None:
    uid = msg.chat.id  # бот работает только в личных чатах: id чата == id пользователя
    ok, limit = await app.check_limit(uid)
    await state.clear()
    if not ok:
        await msg.answer(
            app.limit_text(limit),
            reply_markup=kb([btn("📋 Мои мониторинги", "menu:list")], [btn("🏠 В меню", "menu:home")]),
        )
        return
    mid = await app.db.add_monitor(uid, "filter", filter_title(f), f)
    row = await app.db.get_monitor(uid, mid)
    await msg.answer(
        "✅ <b>Мониторинг создан и запущен!</b>\n\n" + monitor_card(row) + "\n\n" + FIRST_PASS_NOTE,
        reply_markup=monitor_kb(row),
    )


async def run_search(msg: Message, bot: Bot, app: App, f: dict) -> None:
    wait = await msg.answer("🔎 Ищу объявления…")
    watch = kb([btn("🔔 Следить за этим поиском", "search:watch")], [btn("🏠 В меню", "menu:home")])
    try:
        ads = await search_adverts(app.client, app.catalog, f)
    except AvByError as e:
        log.warning("Поиск не удался: %s", e)
        await safe_edit(wait, "⚠️ av.by сейчас не отвечает. Попробуйте чуть позже.", to_menu())
        return
    if not ads:
        await safe_edit(
            wait,
            "Среди свежих объявлений ничего не нашлось.\n"
            "Создайте мониторинг — бот пришлёт подходящие, как только они появятся.",
            watch,
        )
        return
    try:
        await wait.delete()
    except TelegramBadRequest:
        pass
    shown = ads[:5]
    for ad in shown:
        await send_advert(bot, app.db, msg.chat.id, ad, header="🔎 <b>Результат поиска</b>")
    await msg.answer(f"Показано {len(shown)} из {len(ads)}.", reply_markup=watch)


async def finish(msg: Message, state: FSMContext, bot: Bot, app: App) -> None:
    data = await state.get_data()
    f = clean_filters(data["f"])
    if data["mode"] == "search":
        await state.set_state(None)  # данные (фильтр) остаются — нужны для кнопки «Следить за этим поиском»
        await run_search(msg, bot, app, f)
    else:
        await create_monitor_from(msg, state, app, f)


# --- вход: пункты главного меню ------------------------------------------------------------

@router.callback_query(F.data.in_({"menu:add", "menu:search"}))
async def begin(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    mode = "add" if cb.data == "menu:add" else "search"
    await cb.answer()
    if mode == "add":
        ok, limit = await app.check_limit(cb.from_user.id)
        if not ok:  # лучше сказать про лимит сразу, а не после 12 вопросов
            await state.clear()
            await safe_edit(
                cb.message, app.limit_text(limit),
                kb([btn("📋 Мои мониторинги", "menu:list")], [btn("🏠 В меню", "menu:home")]),
            )
            return
    await state.clear()
    await state.update_data(mode=mode, f=copy.deepcopy(DEFAULT_F), pid=None)
    await ask(cb.message, state, app, "brand", edit=True)


@router.callback_query(F.data == "search:watch")
async def watch_search(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    f = (await state.get_data()).get("f")
    await cb.answer()
    if not f:
        await safe_edit(cb.message, "Параметры поиска потерялись — запустите поиск заново.", to_menu())
        return
    await create_monitor_from(cb.message, state, app, clean_filters(f))


# --- навигация ---------------------------------------------------------------------------

@router.callback_query(F.data == "w:cancel")
async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.answer("Отменено")
    await safe_edit(cb.message, WELCOME, main_menu())


@router.callback_query(F.data == "w:noop")
async def on_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data == "w:back")
async def on_back(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    step = await cur_step(state)
    if step is None:
        await expired(cb, state)
        return
    steps = steps_for(await state.get_data())
    i = steps.index(step)
    await cb.answer()
    if i == 0:
        return
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    await ask(cb.message, state, app, steps[i - 1])


@router.callback_query(F.data == "w:skip")
async def on_skip(cb: CallbackQuery, state: FSMContext, bot: Bot, app: App) -> None:
    step = await cur_step(state)
    if step is None:
        await expired(cb, state)
        return
    if step not in SKIPPABLE:
        await cb.answer()
        return
    f = dict((await state.get_data())["f"])
    f[step] = [] if step == "stopwords" else None
    await state.update_data(f=f)
    await cb.answer()
    await collapse(bot, cb.message.chat.id, state, f"{LABEL[step]}: <b>{'нет' if step == 'stopwords' else 'любой'}</b>")
    await advance(cb.message, state, bot, app)


# --- марка / модель ------------------------------------------------------------------------

async def apply_choice(msg: Message, state: FSMContext, bot: Bot, app: App, value: str) -> None:
    step = await cur_step(state)
    f = dict((await state.get_data())["f"])
    if step == "brand":
        f["brand"], f["model"] = value, None  # при смене марки модель сбрасывается
    else:
        f["model"] = value
    await state.update_data(f=f, unk=None)
    await collapse(bot, msg.chat.id, state, f"{LABEL[step]}: <b>{esc(value)}</b>")
    await advance(msg, state, bot, app)


@router.callback_query(StateFilter(W.brand, W.model), F.data.startswith("w:page:"))
async def on_page(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    step = await cur_step(state)
    data = await state.get_data()
    try:
        page = int(cb.data.split(":")[2])
    except ValueError:
        page = 0
    _, rows = await build(step, data, state, app, page)  # type: ignore[arg-type]
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(*rows))
    except TelegramBadRequest:
        pass


@router.callback_query(StateFilter(W.brand, W.model), F.data.startswith("w:pick:"))
async def on_pick(cb: CallbackQuery, state: FSMContext, bot: Bot, app: App) -> None:
    data = await state.get_data()
    names = data.get("names") or []
    try:
        i = int(cb.data.split(":")[2])
    except ValueError:
        i = -1
    if not 0 <= i < len(names):
        await cb.answer("Список устарел — откройте шаг заново.", show_alert=True)
        return
    await cb.answer()
    if data.get("pid") and cb.message.message_id != data["pid"]:  # выбор из подсказки «Уточните»
        try:
            await cb.message.delete()
        except TelegramBadRequest:
            pass
    await apply_choice(cb.message, state, bot, app, names[i])


@router.callback_query(StateFilter(W.model), F.data == "w:all")
async def on_all_models(cb: CallbackQuery, state: FSMContext, bot: Bot, app: App) -> None:
    f = dict((await state.get_data())["f"])
    f["model"] = None
    await state.update_data(f=f)
    await cb.answer()
    await collapse(bot, cb.message.chat.id, state, "Модель: <b>все</b>")
    await advance(cb.message, state, bot, app)


@router.message(StateFilter(W.brand, W.model), TEXT)
async def on_text_pick(message: Message, state: FSMContext, bot: Bot, app: App) -> None:
    names = (await state.get_data()).get("names") or []
    query = message.text.strip()
    if not names:  # каталог недоступен — принимаем как есть
        await apply_choice(message, state, bot, app, query[:60])
        return
    found = pick_names(query, names)
    if not found:
        if (await state.get_data()).get("unk") == norm(query):  # повторили то же самое — значит, уверены
            await apply_choice(message, state, bot, app, query[:60])
            return
        await state.update_data(unk=norm(query))
        await message.answer(
            f"Не нашёл «{esc(query)}» в списке 🤔 Выберите кнопкой или проверьте написание.\n"
            "Если уверены, что название верное (список может быть неполным), отправьте его ещё раз — приму как есть."
        )
    elif len(found) == 1:
        await apply_choice(message, state, bot, app, found[0])
    else:
        rows = chunk([btn(n, f"w:pick:{names.index(n)}") for n in found], 2)
        await message.answer("Уточните, пожалуйста:", reply_markup=kb(*rows))


# --- регион / валюта -----------------------------------------------------------------------

@router.callback_query(StateFilter(W.region), F.data.startswith("w:rg:"))
async def on_region(cb: CallbackQuery, state: FSMContext, bot: Bot, app: App) -> None:
    key = cb.data.split(":")[2]
    f = dict((await state.get_data())["f"])
    f["region"] = None if key == "all" or key not in REGIONS else key
    await state.update_data(f=f)
    await cb.answer()
    await collapse(bot, cb.message.chat.id, state, f"Регион: <b>{REGIONS.get(f['region'] or '', 'вся Беларусь')}</b>")
    await advance(cb.message, state, bot, app)


@router.callback_query(StateFilter(W.currency), F.data.startswith("w:cur:"))
async def on_currency(cb: CallbackQuery, state: FSMContext, bot: Bot, app: App) -> None:
    cur = cb.data.split(":")[2]
    if cur not in ("USD", "BYN"):
        await cb.answer()
        return
    f = dict((await state.get_data())["f"])
    f["currency"] = cur
    await state.update_data(f=f)
    await cb.answer()
    await collapse(bot, cb.message.chat.id, state, f"Валюта: <b>{cur}</b>")
    await advance(cb.message, state, bot, app)


# --- числа / минус-слова -------------------------------------------------------------------

@router.message(StateFilter(W.year_from, W.year_to, W.price_from, W.price_to), TEXT)
async def on_number(message: Message, state: FSMContext, bot: Bot, app: App) -> None:
    step = await cur_step(state)
    raw = message.text.strip()
    f = dict((await state.get_data())["f"])
    if raw in ("-", "—", "–"):
        val = None
    else:
        digits = re.sub(r"[\s_.,]", "", raw)  # «18 500», «18,500», «18.500» -> 18500
        if not digits.isdigit():
            await message.answer("Нужно число, например <code>2015</code>, или «-», чтобы пропустить.")
            return
        val = int(digits)
        error = validate_number(step, val, f)  # type: ignore[arg-type]
        if error:
            await message.answer(error)
            return
    f[step] = val
    await state.update_data(f=f)
    shown = "любой" if val is None else (str(val) if step.startswith("year") else f"{val:,}".replace(",", " "))
    await collapse(bot, message.chat.id, state, f"{LABEL[step]}: <b>{shown}</b>")
    await advance(message, state, bot, app)


@router.message(StateFilter(W.stopwords), TEXT)
async def on_stopwords(message: Message, state: FSMContext, bot: Bot, app: App) -> None:
    raw = message.text.strip()
    words: list[str] = []
    if raw not in ("-", "—", "–"):
        words = [w.strip()[:40] for w in re.split(r"[,;\n]+", raw) if w.strip()][:30]
    f = dict((await state.get_data())["f"])
    f["stopwords"] = words
    await state.update_data(f=f)
    await collapse(bot, message.chat.id, state, f"Минус-слова: <b>{esc(', '.join(words)) or 'нет'}</b>")
    await advance(message, state, bot, app)


# --- множественный выбор: топливо / КПП / кузов ---------------------------------------------

@router.callback_query(StateFilter(W.fuel, W.gearbox, W.body), F.data.startswith("w:tg:"))
async def on_toggle(cb: CallbackQuery, state: FSMContext, app: App) -> None:
    step = await cur_step(state)
    code = cb.data.split(":", 2)[2]
    data = await state.get_data()
    if code not in MULTI[step][1]:  # type: ignore[index]
        await cb.answer()
        return
    f = dict(data["f"])
    chosen = list(f[step])
    if code in chosen:
        chosen.remove(code)
    else:
        chosen.append(code)
    f[step] = chosen
    await state.update_data(f=f)
    data["f"] = f
    _, rows = await build(step, data, state, app)  # type: ignore[arg-type]
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(*rows))
    except TelegramBadRequest:
        pass


@router.callback_query(StateFilter(W.fuel, W.gearbox, W.body), F.data == "w:done")
async def on_done(cb: CallbackQuery, state: FSMContext, bot: Bot, app: App) -> None:
    step = await cur_step(state)
    f = (await state.get_data())["f"]
    names = MULTI[step][1]  # type: ignore[index]
    chosen = ", ".join(names[c] for c in f[step]) or "любой"
    await cb.answer()
    await collapse(bot, cb.message.chat.id, state, f"{LABEL[step]}: <b>{chosen}</b>")  # type: ignore[index]
    await advance(cb.message, state, bot, app)


@router.callback_query(F.data.startswith("w:"))
async def stale_wizard_button(cb: CallbackQuery) -> None:
    await cb.answer("Эта кнопка уже неактуальна.")
