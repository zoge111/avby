"""Тексты сообщений: уведомление об объявлении, карточка мониторинга, заголовки."""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .models import BODIES, FUELS, GEARBOXES, REGIONS, Advert

MINSK_TZ = timezone(timedelta(hours=3))  # в Беларуси нет перехода на летнее время

ALERT_HEADER = "🚗 <b>Найдено новое объявление!</b>"
WELCOME = (
    "🚗 <b>AV Monitor</b>\n\n"
    "Слежу за новыми объявлениями на av.by и сразу присылаю те, что подходят под ваши фильтры.\n\n"
    "Выберите действие:"
)
FIRST_PASS_NOTE = (
    "ℹ️ Текущие объявления бот уже запомнил — уведомления будут только о <b>новых</b>, "
    "опубликованных с этого момента."
)


def esc(s: object) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def num(n: int | float) -> str:
    """18500 -> '18 500'."""
    return f"{int(n):,}".replace(",", " ")


def _cut(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rstrip() + "…"


def alert_text(ad: Advert, header: str = ALERT_HEADER) -> str:
    """Уведомление в формате из ТЗ (HTML)."""
    title = esc(ad.title or f"Объявление №{ad.id}")
    url = html.escape(ad.url, quote=True)
    lines = [header, "", f'<b><a href="{url}">{title}</a></b>']

    if ad.price_usd is not None:
        price = f"💰 <b>{num(ad.price_usd)} USD</b>"
        if ad.price_byn is not None:
            price += f" (~{num(ad.price_byn)} BYN)"
        lines.append(price)
    elif ad.price_byn is not None:
        lines.append(f"💰 <b>{num(ad.price_byn)} BYN</b>")

    row = []
    if ad.location:
        row.append(f"📍 {esc(ad.location)}")
    engine = []
    if ad.engine_capacity:
        engine.append(f"{ad.engine_capacity:.1f} л")
    fuel = FUELS.get(ad.fuel) or ad.fuel_raw
    if fuel:
        engine.append(esc(fuel.capitalize()))
    gearbox = GEARBOXES.get(ad.gearbox) or ad.gearbox_raw
    if gearbox:
        engine.append(esc(gearbox.capitalize()))
    if engine:
        row.append("⚙️ " + ", ".join(engine))
    if row:
        lines.append(" | ".join(row))
    if ad.mileage_km:
        lines.append(f"🛣 Пробег: {num(ad.mileage_km)} км")

    if ad.description:
        lines += ["", f"📝 <i>Описание:</i> {esc(_cut(ad.description, 200))}"]
    lines += ["", f'🔗 <a href="{url}">Открыть на av.by</a>']
    return "\n".join(lines)


def _range(lo: Any, hi: Any, fmt: Callable[[Any], str] = num) -> str:
    if lo is not None and hi is not None:
        return f"{fmt(lo)}–{fmt(hi)}"
    if lo is not None:
        return f"от {fmt(lo)}"
    if hi is not None:
        return f"до {fmt(hi)}"
    return ""


def filter_title(f: dict[str, Any]) -> str:
    """Короткое название мониторинга для списков."""
    if f.get("seller_id") or f.get("seller_name"):
        return f"Продавец: {f.get('seller_name') or f.get('seller_id')}"
    parts = [" ".join(x for x in (f.get("brand"), f.get("model")) if x) or "Любая марка"]
    years = _range(f.get("year_from"), f.get("year_to"), str)
    if years:
        parts.append(years)
    price = _range(f.get("price_from"), f.get("price_to"))
    if price:
        parts.append(f"{price} {f.get('currency', 'USD')}")
    if f.get("region"):
        parts.append(REGIONS.get(f["region"], ""))
    return " · ".join(p for p in parts if p)


def monitor_card(row: dict[str, Any]) -> str:
    """Карточка мониторинга со всеми параметрами."""
    f = row["filters"]
    state = "🟢 работает" if row["is_active"] else "⏸ на паузе"
    lines = [f"🔎 <b>Мониторинг #{row['id']}</b> — {state}", ""]

    if f.get("seller_id") or f.get("seller_name"):
        lines.append(f"👤 Продавец: <b>{esc(f.get('seller_name') or f.get('seller_id'))}</b>")
        if f.get("seller_id"):
            lines.append(f"🆔 ID: <code>{esc(f['seller_id'])}</code>")
        else:
            lines.append("<i>Сопоставление по названию продавца.</i>")
    else:
        car = " ".join(x for x in (f.get("brand"), f.get("model")) if x)
        if f.get("brand") and not f.get("model"):
            car += " (все модели)"
        lines.append(f"🚗 Авто: <b>{esc(car) if car else 'любая марка'}</b>")
        lines.append(f"📍 Регион: {REGIONS.get(f.get('region') or '', 'вся Беларусь')}")
        lines.append(f"📅 Год: {_range(f.get('year_from'), f.get('year_to'), str) or 'любой'}")
        price = _range(f.get("price_from"), f.get("price_to"))
        lines.append(f"💰 Цена: {price + ' ' + f.get('currency', 'USD') if price else 'любая'}")
        for key, title, names in (
            ("fuel", "⛽ Топливо", FUELS), ("gearbox", "⚙️ КПП", GEARBOXES), ("body", "🚙 Кузов", BODIES),
        ):
            vals = f.get(key) or []
            if vals:
                lines.append(f"{title}: {', '.join(names.get(v, v) for v in vals)}")
        if f.get("stopwords"):
            lines.append(f"🚫 Минус-слова: {esc(', '.join(f['stopwords']))}")

    created = datetime.fromtimestamp(row["created_at"], MINSK_TZ)
    lines.append(f"🕒 Создан: {created:%d.%m.%Y %H:%M}")
    return "\n".join(lines)
