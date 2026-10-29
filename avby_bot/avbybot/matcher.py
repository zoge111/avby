"""Сопоставление объявления с фильтром мониторинга (чистая функция).

Формат фильтра (JSON в таблице monitors):
  brand, model        — названия как на av.by (сравниваются без учёта регистра и знаков)
  region              — ключ из models.REGIONS
  year_from, year_to  — границы года
  currency            — "USD" | "BYN"; price_from, price_to — границы цены в этой валюте
  fuel, gearbox, body — списки кодов (models.FUELS / GEARBOXES / BODIES)
  stopwords           — минус-слова; ищутся как подстроки в заголовке и описании
  seller_id, seller_name — мониторинг продавца (остальные поля тогда не используются)
"""
from __future__ import annotations

from typing import Any

from .models import Advert, fold, norm, region_of


def _in_range(value: int | None, lo: int | None, hi: int | None) -> bool:
    if lo is None and hi is None:
        return True
    if value is None:
        return False
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def _seller_ok(f: dict[str, Any], ad: Advert) -> bool:
    sid, sname = f.get("seller_id"), f.get("seller_name")
    if sid and ad.seller_id:
        return str(sid) == str(ad.seller_id)
    want, got = norm(sname), norm(ad.seller_name)
    if not want or not got:
        return False
    return want == got or (len(want) >= 4 and want in got)


def matches(f: dict[str, Any], ad: Advert) -> bool:
    """True, если объявление подходит под фильтр."""
    if f.get("seller_id") or f.get("seller_name"):
        return _seller_ok(f, ad)

    if f.get("brand") and norm(f["brand"]) != norm(ad.brand):
        return False
    if f.get("model") and norm(f["model"]) != norm(ad.model):
        return False

    if f.get("region"):
        got = region_of(ad.location)
        if got is not None and got != f["region"]:
            return False

    if not _in_range(ad.year, f.get("year_from"), f.get("year_to")):
        return False

    price = ad.price_usd if f.get("currency", "USD") == "USD" else ad.price_byn
    if not _in_range(price, f.get("price_from"), f.get("price_to")):
        return False

    for key in ("fuel", "gearbox", "body"):
        want = f.get(key)
        if want and getattr(ad, key) not in want:
            return False

    stop = [fold(w) for w in (f.get("stopwords") or []) if fold(w)]
    if stop:
        text = fold(f"{ad.title} {ad.description}")
        if any(w in text for w in stop):
            return False
    return True
