"""
Проверка VIN номеров авто и кадастровых номеров
"""
import httpx, re, asyncio
from playwright.async_api import async_playwright


def extract_vin(text: str) -> str:
    """Извлекает VIN из текста"""
    # VIN — 17 символов, буквы и цифры (без I, O, Q)
    m = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', text.upper())
    return m.group(1) if m else ""


def extract_cadastral(text: str) -> str:
    """Извлекает кадастровый номер из текста"""
    # Формат: NN:NN:NNNNNNN:NN
    m = re.search(r'\b(\d{2}:\d{2}:\d{6,7}:\d+)\b', text)
    return m.group(1) if m else ""


async def check_vin(vin: str) -> dict:
    """Проверяет VIN через открытые источники"""
    result = {
        "vin": vin,
        "found": False,
        "brand": "",
        "model": "",
        "year": "",
        "accidents": "нет данных",
        "restrictions": "нет данных",
        "summary": "VIN не найден",
        "risk": "неизвестно"
    }
    if not vin or len(vin) != 17:
        return result

    try:
        # Проверяем через ГИБДД (открытый API)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://xn--90adear.xn--p1ai/check/auto",
                params={"vin": vin},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            text = resp.text

            # Ищем данные об ограничениях
            if "ограничен" in text.lower():
                result["restrictions"] = "⚠️ Найдены ограничения!"
                result["risk"] = "высокий"
            elif "не найдено" in text.lower():
                result["restrictions"] = "ограничений нет"
                result["risk"] = "низкий"

    except:
        pass

    # Декодируем базовую инфо из VIN
    wmi = vin[:3]  # Производитель
    year_code = vin[9]
    year_map = {
        'A':2010,'B':2011,'C':2012,'D':2013,'E':2014,
        'F':2015,'G':2016,'H':2017,'J':2018,'K':2019,
        'L':2020,'M':2021,'N':2022,'P':2023,'R':2024,
        'S':2025,'1':2001,'2':2002,'3':2003,'4':2004,
        '5':2005,'6':2006,'7':2007,'8':2008,'9':2009,
    }
    result["year"] = str(year_map.get(year_code.upper(), ""))

    result["found"] = True
    result["summary"] = (
        f"VIN: {vin}"
        f"{f' | Год: {result[\"year\"]}' if result['year'] else ''}"
        f" | Ограничения: {result['restrictions']}"
    )

    return result


async def check_cadastral(cadastral_num: str) -> dict:
    """Проверяет кадастровый номер через Росреестр"""
    result = {
        "cadastral": cadastral_num,
        "found": False,
        "address": "",
        "area": "",
        "cadastral_value": 0,
        "category": "",
        "encumbrances": "нет данных",
        "owners_history": "",
        "summary": "не проверено",
        "risk": "неизвестно"
    }
    if not cadastral_num:
        return result

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Публичная кадастровая карта API
            resp = await client.get(
                f"https://pkk.rosreestr.ru/api/features/5",
                params={"text": cadastral_num, "limit": 1, "skip": 0},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://pkk.rosreestr.ru/"
                }
            )
            if resp.status_code == 200:
                import json
                data = resp.json()
                features = data.get("features", [])
                if features:
                    f = features[0]
                    attrs = f.get("attrs", {})
                    result["found"] = True
                    result["address"] = attrs.get("address", "")
                    result["area"] = str(attrs.get("area_value", ""))
                    result["cadastral_value"] = attrs.get("cad_cost", 0)
                    result["category"] = attrs.get("category_type", "")

    except Exception as e:
        pass

    # Если не нашли через API — формируем ссылку для ручной проверки
    if not result["found"]:
        result["summary"] = (
            f"Кадастр: {cadastral_num} | "
            f"Проверить: pkk.rosreestr.ru/?cadastralNumber={cadastral_num}"
        )
    else:
        val = result["cadastral_value"]
        val_str = f"{val/1e6:.1f} млн ₽" if val > 1e6 else f"{val:,.0f} ₽" if val > 0 else "нет"
        result["summary"] = (
            f"Кадастр: {cadastral_num} | "
            f"Площадь: {result['area']} м² | "
            f"Кад. стоимость: {val_str}"
        )
        result["risk"] = "низкий"

    return result


async def get_lot_participants(url: str, page) -> int:
    """Получает количество участников торгов"""
    try:
        # Ищем на странице лота
        text = await page.inner_text("body")
        patterns = [
            r'участник[ов]*[:\s]*(\d+)',
            r'заявк[аи][:\s]*(\d+)',
            r'претендент[ов]*[:\s]*(\d+)',
            r'(\d+)\s*участник',
            r'(\d+)\s*заявк',
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if 0 <= n <= 100:
                    return n
    except:
        pass
    return 0
