"""
Ориентир рынка без парсинга Циан/Авито — только публичная поисковая выдача.
Если цены в сниппетах не найдены — честно «рынок не определён».
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics

log = logging.getLogger("market_search")

# Платные альтернативы (для справки в комментарии):
# - CIAN B2B API — от ~50–150 тыс ₽/мес, по запросу
# - Dadata «Рыночная стоимость» — от ~14 ₽/запрос
# - Яндекс.Недвижимость API — закрытый партнёрский доступ


def market_search_note() -> str:
    return (
        "Прямой парсинг Циан/Авито не используем (капча/баны). "
        "Ориентир — только если цены видны в поисковой выдаче. "
        "Надёжнее: Dadata (~14₽/запрос) или CIAN B2B API."
    )


def _parse_prices_from_text(text: str) -> list[int]:
    prices = []
    for m in re.finditer(
        r"(\d[\d\s]{4,11})\s*(?:₽|руб\.?|RUB)|"
        r"(\d+[.,]\d+)\s*(?:млн|mln)\s*(?:₽|руб\.?)?",
        text, re.I,
    ):
        if m.group(1):
            v = int(re.sub(r"\s", "", m.group(1)))
        else:
            v = int(float(m.group(2).replace(",", ".")) * 1_000_000)
        if 500_000 <= v <= 500_000_000:
            prices.append(v)
    return prices


async def _duckduckgo_snippets(query: str) -> str:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, trust_env=False) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kl": "ru-ru"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if resp.status_code == 200:
                return resp.text
    except Exception as e:
        log.debug("ddg search: %s", e)
    return ""


async def fetch_market_orientir(
    lot_type: str,
    address: str,
    area_sqm: float,
    region: str,
    title: str = "",
) -> dict:
    """
    Пробует найти цены похожих объектов в поисковой выдаче.
    found=False → рынок не определён (без выдуманных цифр).
    """
    if lot_type in ("авто", "земля", "бизнес", "прочее", "гараж"):
        return {"found": False, "comment": "рынок не определён — тип объекта без авто-оценки"}

    if not area_sqm or area_sqm <= 0:
        return {"found": False, "comment": "рынок не определён — площадь неизвестна"}

    loc = (address or title or "").strip()
    loc_short = re.sub(r"\s+", " ", loc)[:60]
    if not loc_short:
        return {"found": False, "comment": "рынок не определён — адрес неизвестен"}

    type_word = {
        "квартира": "квартира",
        "апартаменты": "апартаменты",
        "дом": "дом",
        "коммерция": "коммерческая недвижимость",
    }.get(lot_type, "квартира")

    area_i = int(area_sqm)
    queries = [
        f"купить {type_word} {loc_short} {area_i} м2 цена",
        f"{type_word} {loc_short} {area_i} кв м продажа",
    ]

    all_prices: list[int] = []
    for q in queries:
        html = await _duckduckgo_snippets(q)
        if not html:
            continue
        all_prices.extend(_parse_prices_from_text(html))
        await asyncio.sleep(0.3)

    if len(all_prices) < 2:
        return {
            "found": False,
            "comment": "рынок не определён — в поиске не нашли сопоставимых цен",
        }

    # Отсекаем выбросы
    med = statistics.median(all_prices)
    filtered = [p for p in all_prices if med * 0.4 <= p <= med * 2.5]
    if len(filtered) < 2:
        filtered = all_prices

    price = int(statistics.median(filtered))
    return {
        "found": True,
        "market_price": price,
        "comment": (
            f"ориентир по объявлениям (поиск), проверить — "
            f"медиана {len(filtered)} цен в выдаче"
        ),
        "source": "search",
        "price_per_sqm": int(price / area_sqm) if area_sqm else 0,
    }
