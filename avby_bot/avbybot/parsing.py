"""Разбор данных av.by: чистые функции без сети и без aiogram.

ВАЖНО: формат ответов av.by не документирован и может меняться. Все допущения о названиях
полей собраны в этом файле. Если уведомления «пустые» или объявления не находятся —
запустите `python -m avbybot.check_api`: он покажет, какие поля удалось разобрать.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any

from .models import Advert, fold, parse_body, parse_fuel, parse_gearbox

WEB = "https://cars.av.by"
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


class AvByError(Exception):
    """Ошибка получения или разбора данных av.by."""


# --- поиск данных в произвольном JSON --------------------------------------------------

_NEXT_RE = re.compile(r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S)


def extract_next_data(page_html: str) -> Any:
    """Достаёт JSON из <script id="__NEXT_DATA__"> (так сайт отдаёт начальное состояние страницы)."""
    m = _NEXT_RE.search(page_html)
    if not m:
        raise AvByError("в HTML нет __NEXT_DATA__ (формат сайта изменился или сработала защита от ботов)")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise AvByError(f"__NEXT_DATA__ не разобрался: {e}") from e


def _looks_like_adverts(lst: list) -> bool:
    if not all(isinstance(x, dict) for x in lst):
        return False
    hits = sum(1 for x in lst if "id" in x and ("publicUrl" in x or "properties" in x))
    return hits >= max(1, len(lst) // 2)


def find_adverts(obj: Any) -> list[dict] | None:
    """Ищет в JSON список объявлений (ключ "adverts" или список, похожий на объявления).

    None — структура не похожа на ленту (например, сайт отдал заглушку); [] — лента пуста.
    Так код не привязан к точному пути вида props.initialState.filter.main.adverts.
    """
    found: list[dict] = []
    ok = False
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key, val in cur.items():
                if key == "adverts" and isinstance(val, list):
                    ok = True
                    found.extend(x for x in val if isinstance(x, dict))
                elif isinstance(val, (dict, list)):
                    stack.append(val)
        elif isinstance(cur, list):
            if cur and _looks_like_adverts(cur):
                ok = True
                found.extend(cur)
            else:
                stack.extend(x for x in cur if isinstance(x, (dict, list)))
    if not ok:
        return None
    seen: set[Any] = set()
    out: list[dict] = []
    for a in found:
        key = a.get("id")
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def find_advert(obj: Any, ad_id: int) -> dict | None:
    """Ищет в JSON карточку конкретного объявления по id."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            try:
                cid = int(cur.get("id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                cid = None
            if cid == ad_id and ("properties" in cur or "sellerName" in cur or "publicUrl" in cur):
                return cur
            stack.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return None


def looks_sorted_desc(ads: list[Advert]) -> bool | None:
    """Отсортирована ли лента «новые сверху». None — мало данных для вывода."""
    ts = [a.published_at for a in ads if a.published_at]
    if len(ts) < 6:
        return None
    good = sum(1 for x, y in zip(ts, ts[1:]) if x >= y)
    return good / (len(ts) - 1) >= 0.8


# --- нормализация одного объявления -----------------------------------------------------

def _text(v: Any) -> str:
    if isinstance(v, dict):
        v = v.get("name") or v.get("label") or v.get("value") or ""
    return "" if v is None else str(v).strip()


def _clean(v: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(_text(v))).strip()


def _int(v: Any) -> int | None:
    if isinstance(v, dict):
        v = v.get("value") or v.get("amount")
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"\d+", re.sub(r"[\s\u00a0\u2009]", "", str(v)))
    return int(m.group()) if m else None


def _amount(v: Any) -> int | None:
    return _int(v.get("amount")) if isinstance(v, dict) else _int(v)


def _capacity(v: Any) -> float | None:
    """Объём двигателя в литрах (понимает и «2.0», и «1998» см³)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        m = re.search(r"\d+(?:[.,]\d+)?", str(v))
        if not m:
            return None
        x = float(m.group().replace(",", "."))
    if x >= 100:
        x /= 1000
    return round(x, 1) if 0 < x < 20 else None


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    for parse in (datetime.fromisoformat, lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%S%z")):
        try:
            d = parse(s)
        except ValueError:
            continue
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _props(p: Any) -> dict[str, Any]:
    """properties: [{"name": "brand", "value": "Audi"}, ...] -> {"brand": "Audi"} (ключи в нижнем регистре)."""
    out: dict[str, Any] = {}
    if isinstance(p, list):
        for it in p:
            if isinstance(it, dict) and "value" in it:
                key = it.get("name") or it.get("key") or it.get("id")
                if key is not None:
                    out[fold(key)] = it["value"]
    elif isinstance(p, dict):
        out = {fold(k): v for k, v in p.items()}
    return out


def _pick(props: dict[str, Any], *names: str) -> Any:
    for n in names:
        v = props.get(fold(n))
        if v not in (None, "", []):
            return v
    return None


def _photo(v: Any) -> str:
    if not isinstance(v, list) or not v:
        return ""
    first = v[0]
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        for k in ("big", "medium", "small", "extraBig", "original"):
            x = first.get(k)
            if isinstance(x, dict) and x.get("url"):
                return str(x["url"])
            if isinstance(x, str) and x.startswith("http"):
                return x
        if first.get("url"):
            return str(first["url"])
    return ""


def _seller_id(raw: dict) -> str:
    for k in ("sellerId", "seller_id", "userId", "user_id", "ownerId"):
        if raw.get(k):
            return str(raw[k])
    for k in ("seller", "user", "owner", "author"):
        d = raw.get(k)
        if isinstance(d, dict) and d.get("id"):
            return str(d["id"])
    return ""


def _url(raw: dict, ad_id: int) -> str:
    url = _text(raw.get("publicUrl") or raw.get("url") or raw.get("link"))
    if url.startswith("/"):
        url = WEB + url
    return url or f"{WEB}/{ad_id}"


def normalize(raw: Any) -> Advert | None:
    """Приводит «сырое» объявление av.by к Advert. None — если это не объявление."""
    if not isinstance(raw, dict):
        return None
    try:
        ad_id = int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None
    p = _props(raw.get("properties"))

    fuel_raw = _text(_pick(p, "engine_type", "fuel_type", "fuel", "тип двигателя", "топливо"))
    gear_raw = _text(_pick(p, "transmission_type", "transmission", "gearbox", "коробка передач"))
    body_raw = _text(_pick(p, "body_type", "body", "тип кузова", "кузов"))

    price = raw.get("price") if isinstance(raw.get("price"), dict) else {}

    return Advert(
        id=ad_id,
        url=_url(raw, ad_id),
        brand=_text(_pick(p, "brand", "марка") or raw.get("brand")),
        model=_text(_pick(p, "model", "модель") or raw.get("model")),
        generation=_text(_pick(p, "generation", "поколение") or raw.get("generation")),
        year=_int(_pick(p, "year", "год", "год выпуска") or raw.get("year")),
        price_usd=_amount(price.get("usd")),
        price_byn=_amount(price.get("byn")),
        location=_text(raw.get("locationName") or raw.get("shortLocationName") or raw.get("location")),
        mileage_km=_int(_pick(p, "mileage_km", "mileage", "пробег")),
        engine_capacity=_capacity(_pick(p, "engine_capacity", "engine_volume", "объем двигателя", "объём двигателя")),
        fuel=parse_fuel(fuel_raw), fuel_raw=fuel_raw,
        gearbox=parse_gearbox(gear_raw), gearbox_raw=gear_raw,
        body=parse_body(body_raw), body_raw=body_raw,
        description=_clean(raw.get("description")),
        seller_name=_text(raw.get("sellerName") or raw.get("seller")),
        seller_id=_seller_id(raw),
        photo=_photo(raw.get("photos")),
        published_at=_dt(raw.get("publishedAt") or raw.get("createdAt")),
    )
