"""
Реальные рыночные цены — Авито и Циан через поиск
"""
import httpx, re, asyncio, os
from dotenv import load_dotenv
load_dotenv()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = os.getenv("GROQ_API_KEY")
MODEL    = "llama-3.1-8b-instant"


async def search_avito_price(query: str) -> list:
    """Ищет цены на Авито через HTTP"""
    prices = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        search_url = f"https://www.avito.ru/rossiya?q={query.replace(' ', '+')}&s=104"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(search_url, headers=headers)
            text = resp.text
            # Ищем цены в ответе
            price_matches = re.findall(r'"price":\s*\{[^}]*"value":\s*(\d+)', text)
            for p in price_matches[:10]:
                try:
                    price = int(p)
                    if 100_000 < price < 500_000_000:
                        prices.append(price)
                except:
                    pass
    except Exception as e:
        pass
    return sorted(prices)


async def get_market_price(lot_type: str, title: str, region: str, area: float = 0) -> dict:
    """
    Получает реальную рыночную цену через:
    1. Попытку парсинга Авито
    2. Оценку через Groq на основе знания рынка
    """
    region_name = "Москва" if "moskva" in region else "Московская область"

    # Формируем поисковый запрос
    area_str = f"{int(area)} м²" if area > 0 else ""
    search_queries = {
        "квартира": f"квартира {area_str} {region_name}",
        "дом":      f"дом {area_str} {region_name}",
        "коммерция":f"нежилое помещение {area_str} {region_name}",
        "земля":    f"земельный участок {region_name}",
        "авто":     title[:50],
        "гараж":    f"гараж {region_name}",
    }
    query = search_queries.get(lot_type, title[:50])

    # Пробуем Авито
    avito_prices = await search_avito_price(query)

    # Оценка через Groq с учётом данных Авито
    avito_context = ""
    if avito_prices:
        avg = sum(avito_prices) / len(avito_prices)
        avito_context = f"\nДанные Авито (найдено {len(avito_prices)} объявлений): средняя цена {avg:,.0f} руб, диапазон {min(avito_prices):,.0f} — {max(avito_prices):,.0f} руб"

    prompt = f"""Ты эксперт по рынку недвижимости и имущества России.
Определи реальную рыночную стоимость объекта.

Объект: {title[:150]}
Тип: {lot_type}
Регион: {region_name}
{f'Площадь: {area} м²' if area > 0 else ''}
{avito_context}

На основе актуальных данных рынка 2024-2025 года дай оценку.
Ответь ТОЛЬКО в JSON:
{{
  "market_price": 8500000,
  "price_min": 7000000,
  "price_max": 10000000,
  "price_per_sqm": 125000,
  "rental_monthly": 45000,
  "data_source": "Авито + экспертная оценка",
  "confidence": "высокая",
  "comment": "Аналоги в этом районе продаются за 7-10 млн"
}}"""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 300,
                    "temperature": 0.1,
                }
            )
            data = resp.json()
            if "choices" in data:
                raw = data["choices"][0]["message"]["content"]
                import json
                m = re.search(r'\{[\s\S]*\}', raw)
                if m:
                    result = json.loads(m.group())
                    # Если есть данные Авито — корректируем
                    if avito_prices and len(avito_prices) >= 3:
                        avg_avito = sum(avito_prices) / len(avito_prices)
                        # Берём взвешенное среднее (70% Авито + 30% Groq)
                        groq_price = result.get("market_price", avg_avito)
                        result["market_price"] = int(avg_avito * 0.7 + groq_price * 0.3)
                        result["data_source"] = f"Авито ({len(avito_prices)} объявл.) + ИИ"
                        result["confidence"] = "высокая"
                    return result
    except Exception as e:
        pass

    # Заглушка
    return {
        "market_price": 0,
        "price_per_sqm": 0,
        "rental_monthly": 0,
        "data_source": "нет данных",
        "confidence": "низкая",
        "comment": ""
    }
