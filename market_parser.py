"""
Реальный парсинг цен с Циан и Авито
"""
import httpx, re, asyncio, json
from playwright.async_api import async_playwright


async def parse_cian(query: str, lot_type: str) -> list:
    """Парсит реальные объявления с Циан"""
    prices = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )

            # Формируем URL поиска
            type_map = {
                "квартира": "kupit-kvartiru",
                "дом":      "kupit-dom",
                "коммерция":"kupit-kommercheskuyu-nedvizhimost",
                "земля":    "kupit-zemlyu",
                "гараж":    "kupit-garazh",
            }
            cian_type = type_map.get(lot_type, "kupit-kvartiru")

            # Определяем регион из запроса
            if "москв" in query.lower() or "МО" in query:
                region = "moskva"
            else:
                region = "moskovskaya-oblast"

            url = f"https://www.cian.ru/{cian_type}/{region}/"

            await page.goto(url, timeout=20000)
            await page.wait_for_timeout(2000)

            # Ищем цены на странице
            price_els = await page.query_selector_all(
                "[class*='price'], [data-name*='Price'], [class*='Price']"
            )
            for el in price_els[:20]:
                try:
                    text = await el.inner_text()
                    m = re.search(r'(\d[\d\s]+)\s*(?:₽|руб)', text)
                    if m:
                        price = float(re.sub(r'\s', '', m.group(1)))
                        if 500_000 < price < 500_000_000:
                            prices.append(price)
                except:
                    continue

            await browser.close()

    except Exception as e:
        print(f"    Циан: {e}")

    return sorted(prices)


async def parse_avito(query: str, lot_type: str, region: str) -> list:
    """Парсит реальные объявления с Авито"""
    prices = []
    try:
        region_map = {
            "moskva": "moskva",
            "moskovskaya-oblast": "moskovskaya_oblast"
        }
        avito_region = region_map.get(region, "moskva")

        cat_map = {
            "квартира": "nedvizhimost/kvartiry/prodam",
            "дом":      "nedvizhimost/doma_dachi_kottedzhi/prodam",
            "коммерция":"nedvizhimost/kommercheskaya_nedvizhimost",
            "земля":    "nedvizhimost/zemelnye_uchastki",
            "авто":     "avtomobili",
            "гараж":    "nedvizhimost/garazhi_i_mashinomesta",
        }
        cat = cat_map.get(lot_type, "nedvizhimost")

        # Чистим запрос
        clean_query = re.sub(r'[^\w\s]', '', query)[:50]
        url = f"https://www.avito.ru/{avito_region}/{cat}?q={clean_query.replace(' ', '+')}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            text = resp.text

            # Несколько паттернов поиска цен
            patterns = [
                r'"price":\{"value":(\d+)',
                r'data-price="(\d+)"',
                r'"priceValue":(\d+)',
                r'class="price[^"]*"[^>]*>[\s\S]*?(\d[\d\s]+)\s*₽',
            ]
            for pattern in patterns:
                for m in re.finditer(pattern, text):
                    try:
                        price = float(re.sub(r'\s', '', m.group(1)))
                        if 100_000 < price < 500_000_000:
                            prices.append(price)
                    except:
                        pass

    except Exception as e:
        print(f"    Авито: {e}")

    return sorted(prices)


def calc_market_stats(prices: list) -> dict:
    """Считает статистику по ценам"""
    if not prices:
        return {"median": 0, "avg": 0, "min": 0, "max": 0, "count": 0}

    prices = sorted(prices)
    n = len(prices)
    # Убираем выбросы (топ и боттом 10%)
    cut = max(1, n // 10)
    clean = prices[cut:-cut] if n > 10 else prices

    return {
        "median": clean[len(clean)//2],
        "avg":    sum(clean) / len(clean),
        "min":    min(clean),
        "max":    max(clean),
        "count":  n,
    }


async def get_real_market_price(lot_type: str, title: str,
                                region: str, area: float = 0) -> dict:
    """
    Получает реальную рыночную цену:
    1. Парсит Авито
    2. Парсит Циан
    3. Считает медиану
    4. Считает цену/м² если есть площадь
    """
    print(f"    📊 Парсим рынок: {lot_type} {region}...")

    # Формируем запрос для поиска
    area_str = f"{int(area)}м" if area > 0 else ""
    queries = {
        "квартира": f"квартира {area_str}",
        "дом":      f"дом {area_str}",
        "коммерция":f"помещение {area_str}",
        "земля":    "участок",
        "авто":     title[:40],
        "гараж":    "гараж",
    }
    query = queries.get(lot_type, title[:40])

    # Параллельный парсинг
    avito_task = parse_avito(query, lot_type, region)
    cian_task  = parse_cian(query, lot_type)

    avito_prices, cian_prices = await asyncio.gather(
        avito_task, cian_task, return_exceptions=True
    )

    if isinstance(avito_prices, Exception): avito_prices = []
    if isinstance(cian_prices,  Exception): cian_prices  = []

    all_prices = avito_prices + cian_prices
    stats = calc_market_stats(all_prices)

    # Источники данных
    sources = []
    if avito_prices: sources.append(f"Авито ({len(avito_prices)})")
    if cian_prices:  sources.append(f"Циан ({len(cian_prices)})")
    data_source = " + ".join(sources) if sources else "нет данных"

    market_price = stats["median"] or stats["avg"] or 0
    price_per_sqm = round(market_price / area) if area > 0 and market_price > 0 else 0

    confidence = "высокая" if len(all_prices) >= 5 else \
                 "средняя" if len(all_prices) >= 2 else "низкая"

    print(f"    ✅ {data_source}: медиана {market_price/1e6:.1f}млн ({len(all_prices)} объявл.)")

    return {
        "market_price":   market_price,
        "price_per_sqm":  price_per_sqm,
        "price_min":      stats["min"],
        "price_max":      stats["max"],
        "listings_count": stats["count"],
        "data_source":    data_source,
        "confidence":     confidence,
        "avito_count":    len(avito_prices),
        "cian_count":     len(cian_prices),
    }
