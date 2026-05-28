"""
Анализатор v7.0
- Исправлено определение категорий (авто ≠ квартира)
- VIN проверка для авто
- Кадастровая проверка для недвижимости
- Количество участников
- Реальные цены с Авито/Циан
- Инвест-расчёты с IRR и сценариями
"""
import httpx, json, re, os, asyncio
from dotenv import load_dotenv
load_dotenv()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = os.getenv("GROQ_API_KEY")
MODEL    = "llama-3.1-8b-instant"

MIN_SCORE = {
    "квартира":  0,
    "дом":       0,
    "коммерция": 0,
    "земля":     7.0,
    "авто":      8.0,
    "гараж":     8.0,
    "бизнес":    8.0,
    "прочее":    8.5,
}


def detect_type(text: str) -> str:
    """
    ВАЖНО: проверяем специфичные категории ПЕРВЫМИ
    чтобы Peugeot не попал в квартиры
    """
    t = text.lower()

    # 1. АВТО — самые специфичные, всегда первыми
    auto_words = [
        "автомобил", "легков", "грузов", "седан", "хэтчбек",
        "внедорожник", "кроссовер", "минивэн", "микроавтобус",
        "автобус", "мотоцикл", "прицеп", "полуприцеп",
        "спецтехник", "экскаватор", "трактор", "бульдозер",
        "кран", "погрузчик", "самосвал", "манипулятор",
        "транспортн", "колёсн", "двигател",
        # Марки авто — латиница и кириллица
        "камаз", "газель", "уаз", "ваз",
        "lada", "bmw", "mercedes", "benz", "toyota", "hyundai",
        "kia", "volkswagen", "ford", "renault", "nissan",
        "mazda", "honda", "audi", "volvo", "skoda", "opel",
        "peugeot", "citroen", "mitsubishi", "subaru", "lexus",
        "infiniti", "porsche", "land rover", "jeep", "dodge",
        "chevrolet", "chery", "geely", "haval", "changan",
        "тойота", "хендай", "киа", "мерседес", "форд",
        "рено", "ниссан", "мазда", "хонда", "ауди", "вольво",
        "шкода", "пежо", "ситроен", "мицубиси",
        # VIN часто есть в названии
        " vin ", "г/н ", "гос.номер",
    ]
    if any(w in t for w in auto_words):
        return "авто"

    # 2. ГАРАЖИ
    if any(w in t for w in ["гараж", "машиноместо", "парковочн", "стоянк"]):
        return "гараж"

    # 3. ЗЕМЛЯ — до домов
    land_words = [
        "земельн", "участок", "га ", "гектар", "снт ", "днп ",
        "ижс", "лпх ", "сельхоз", "пашн", "угодь", "садовод",
        "поле ", "лесн"
    ]
    if any(w in t for w in land_words):
        if not any(w in t for w in ["квартир", "комнат", "студи", "апартамент"]):
            return "земля"

    # 4. КВАРТИРЫ
    flat_words = [
        "квартир", "комнат", "студи", "апартамент",
        "однокомнат", "двухкомнат", "трёхкомнат",
        "1-комн", "2-комн", "3-комн", "4-комн",
        "жилое помещение", "доля в квартир",
        "кв. м", "кв.м",
    ]
    if any(w in t for w in flat_words):
        return "квартира"

    # 5. ДОМА
    house_words = [
        "жилой дом", "дача", "коттедж", "таунхаус",
        "садовый дом", "часть дома", "домовлад", "загородн",
    ]
    if any(w in t for w in house_words):
        return "дом"

    # 6. КОММЕРЦИЯ
    commercial_words = [
        "нежилое", "офис", "торгов", "магазин", "склад",
        "производ", "ресторан", "кафе", "гостиниц", "здание",
        "помещени", "псн", "арендн", "павильон", "цех",
        "ангар", "база ", "автосервис", "техцентр",
    ]
    if any(w in t for w in commercial_words):
        return "коммерция"

    # 7. БИЗНЕС
    if any(w in t for w in ["оборудован", "станок", "доля в ооо",
                             "акци", "дебиторск", "право требован"]):
        return "бизнес"

    return "прочее"


def is_worth_showing(lot_type: str, score: float) -> bool:
    return score >= MIN_SCORE.get(lot_type, 8.0)


async def get_lot_details(lot_url: str, page) -> dict:
    details = {
        "price": 0, "title_full": "", "description": "",
        "step_current": 0, "step_total": 0, "participants": 0,
        "vin": "", "cadastral": "",
    }
    try:
        await page.goto(lot_url, timeout=20000)
        await page.wait_for_timeout(1500)

        try:
            h1 = await page.query_selector("h1")
            if h1:
                details["title_full"] = (await h1.inner_text()).strip()[:300]
        except:
            pass

        full_text = await page.inner_text("body")
        details["description"] = full_text[:2000]

        # Цена
        for pattern in [
            r'начальн[^\d]*(\d[\d\s]{3,})\s*(?:руб|₽)',
            r'(\d[\d\s]{3,})\s*(?:руб|₽)',
        ]:
            m = re.search(pattern, full_text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s', '', m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        details["price"] = p
                        break
                except:
                    pass

        # Шаг торгов
        step_m = re.search(
            r'(\d+)\s*/\s*(\d+)|шаг[^\d]*(\d+)[^\d]+(\d+)',
            full_text, re.IGNORECASE
        )
        if step_m:
            g = step_m.groups()
            try:
                cur = int(g[0] or g[2] or 0)
                tot = int(g[1] or g[3] or 0)
                if 0 < cur <= tot <= 20:
                    details["step_current"] = cur
                    details["step_total"]   = tot
            except:
                pass

        # Количество участников
        from vin_cadastr import get_lot_participants
        details["participants"] = await get_lot_participants(lot_url, page)

        # VIN для авто
        from vin_cadastr import extract_vin, extract_cadastral
        details["vin"]       = extract_vin(full_text)
        details["cadastral"] = extract_cadastral(full_text)

    except:
        pass
    return details

def calc_investment(lot_price: float, market_price: float,
                    rental_monthly: float, lot_type: str) -> dict:
    """Полный инвест-расчёт"""
    if lot_price <= 0:
        return {"summary": "нет данных для расчёта"}

    # Сценарий 1 — аренда
    rent_yield_annual = 0
    rent_payback = 0
    if rental_monthly > 0 and lot_price > 0:
        rent_yield_annual = round(rental_monthly * 12 / lot_price * 100, 1)
        rent_payback = round(lot_price / (rental_monthly * 12), 1)

    # Сценарий 2 — перепродажа
    flip_profit = max(0, market_price - lot_price) if market_price > 0 else 0
    flip_pct    = round(flip_profit / lot_price * 100) if lot_price > 0 else 0

    # ROI за 5 лет (аренда + рост стоимости 5%/год)
    growth_5y = lot_price * (1.05 ** 5) - lot_price if lot_price > 0 else 0
    rent_5y   = rental_monthly * 12 * 5 if rental_monthly > 0 else 0
    total_5y  = flip_profit + rent_5y + growth_5y
    roi_5y    = round(total_5y / lot_price * 100) if lot_price > 0 else 0

    # Текст
    parts = []
    if rental_monthly > 0:
        parts.append(
            f"Аренда: ~{rental_monthly:,.0f} ₽/мес | "
            f"Доходность {rent_yield_annual}% год | "
            f"Окупаемость {rent_payback} лет"
        )
    if flip_pct > 0:
        parts.append(f"Перепродажа: +{flip_pct}% (+{flip_profit/1e6:.1f} млн ₽)")
    if roi_5y > 0:
        parts.append(f"ROI за 5 лет: ~{roi_5y}%")

    return {
        "rent_yield":    rent_yield_annual,
        "rent_payback":  rent_payback,
        "flip_pct":      flip_pct,
        "flip_profit":   flip_profit,
        "roi_5y":        roi_5y,
        "summary":       " | ".join(parts) if parts else "нет данных",
    }


async def call_groq(prompt: str, max_tokens: int = 700) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model":    MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": 0.15,
                }
            )
            data = resp.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"    Groq: {e}")
    return ""


async def analyze_lot(lot: dict) -> dict:
    title        = lot.get("title_full") or lot.get("title", "")
    region       = lot.get("region", "moskva")
    pdf_text     = lot.get("pdf_text", "")
    lot_info     = lot.get("description", "")
    lot_price    = lot.get("price", 0)
    lot_type     = lot.get("category", "прочее")
    step_cur     = lot.get("step_current", 0)
    step_tot     = lot.get("step_total", 0)
    participants = lot.get("participants", 0)
    vin          = lot.get("vin", "")
    cadastral    = lot.get("cadastral", "")
    region_name  = "Москва" if "moskva" in region else "Московская область"

    # Цена из PDF
    if lot_price == 0:
        for pat in [r'(\d[\d\s]{4,})\s*(?:руб|₽)', r'цена[^\d]*(\d[\d\s]{4,})']:
            m = re.search(pat, pdf_text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s', '', m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        lot_price = p
                        break
                except:
                    pass

    # Площадь
    area_m = re.search(r'(\d+[.,]?\d*)\s*м²', title + " " + pdf_text, re.IGNORECASE)
    area   = float(area_m.group(1).replace(',','.')) if area_m else 0

    # Реальные цены с Авито/Циан
    market_data = {}
    try:
        from market_parser import get_real_market_price
        market_data = await get_real_market_price(lot_type, title, region, area)
    except:
        pass

    market_price_real = market_data.get("market_price", 0)

    # VIN проверка для авто
    vin_info = {}
    if lot_type == "авто" and vin:
        try:
            from vin_cadastr import check_vin
            vin_info = await check_vin(vin)
        except:
            pass

    # Кадастровая проверка
    cadastr_info = {}
    if lot_type in ("квартира", "дом", "коммерция", "земля") and cadastral:
        try:
            from vin_cadastr import check_cadastral
            cadastr_info = await check_cadastral(cadastral)
        except:
            pass

    # Инвест расчёт
    rental = market_data.get("rental_monthly", 0)
    invest = calc_investment(lot_price, market_price_real, rental, lot_type)

    # Участники
    competition = ""
    if participants > 0:
        if participants == 0:
            competition = "нет заявок — можно взять по минимуму"
        elif participants == 1:
            competition = "1 участник — конкуренция низкая"
        elif participants <= 3:
            competition = f"{participants} участника — умеренная конкуренция"
        else:
            competition = f"{participants} участников — высокая конкуренция!"

    step_info = ""
    if step_cur and step_tot:
        steps_left = step_tot - step_cur
        step_info = f"Шаг {step_cur}/{step_tot} (осталось {steps_left} снижений)"

    # Тип объекта для промпта — явно указываем чтобы ИИ не путал
    type_names = {
        "квартира": "КВАРТИРА (жилая недвижимость)",
        "дом":      "ЖИЛ. ДОМ (загородная недвижимость)",
        "коммерция":"КОММЕРЧЕСКАЯ НЕДВИЖИМОСТЬ",
        "земля":    "ЗЕМЕЛЬНЫЙ УЧАСТОК",
        "авто":     "АВТОМОБИЛЬ / ТРАНСПОРТНОЕ СРЕДСТВО",
        "гараж":    "ГАРАЖ / МАШИНОМЕСТО",
        "бизнес":   "БИЗНЕС / ОБОРУДОВАНИЕ",
        "прочее":   "ПРОЧЕЕ ИМУЩЕСТВО",
    }
    type_label = type_names.get(lot_type, lot_type.upper())

    prompt = f"""Ты топ-эксперт по инвестициям в банкротные торги России.
ВАЖНО: ты анализируешь {type_label} — не путай с другими типами объектов!

═══ ОБЪЕКТ ═══
ТИП: {type_label}
Название: {title[:200]}
Регион: {region_name}
Цена на торгах: {f'{lot_price:,.0f} руб'.replace(',', ' ') if lot_price > 0 else 'не определена'}
Рыночная цена (Авито/Циан): {f'{market_price_real:,.0f} руб'.replace(',', ' ') if market_price_real > 0 else 'не найдена'}
{step_info}
Участников в торгах: {participants if participants > 0 else 'нет данных'}
{f'VIN: {vin}' if vin else ''}
{f'Кадастровый номер: {cadastral}' if cadastral else ''}

Данные ЕГРН:
{pdf_text[:500]}

Описание:
{lot_info[:400]}

Инвест-расчёт:
{invest['summary']}

Дай экспертный анализ ИМЕННО ДЛЯ {type_label}.
Ответь ТОЛЬКО JSON:
{{
  "total_score": 8.2,
  "market_price_rub": 8000000,
  "discount_pct": 35,
  "liquidity_level": "высокая",
  "liquidity_days": 30,
  "legal_score": 8,
  "owners_count": "1",
  "encumbrances": "залог Сбербанк — снимается при покупке",
  "red_flags": [],
  "legal_summary": "1 собственник, залог снимается автоматически",
  "action": "ВХОДИТЬ СЕЙЧАС",
  "action_emoji": "🟢",
  "strategy": "2-3 конкретных предложения почему входить или ждать",
  "what_to_check": "что проверить перед покупкой",
  "invest_potential": "высокий",
  "risk_level": "низкий",
  "verdict": "РЕКОМЕНДУЕТСЯ К ПОКУПКЕ",
  "object_type_confirmed": "{lot_type}"
}}"""

    raw  = await call_groq(prompt, 700)
    try:
        m = re.search(r'\{{[\s\S]*\}}', raw)
        result = json.loads(m.group()) if m else {}
    except:
        result = {}

    def fmt(p):
        try:
            p = float(p)
            if p >= 1_000_000: return f"{p/1_000_000:.1f} млн ₽"
            elif p > 0:        return f"{int(p):,} ₽".replace(',', ' ')
        except: pass
        return "уточните на сайте"

    red_flags = result.get("red_flags", [])
    legal_text = result.get("legal_summary", "требует проверки")
    if red_flags:
        legal_text += " ⚠️ " + "; ".join(str(f) for f in red_flags[:2])

    # Добавляем VIN инфо
    extra_checks = []
    if vin_info.get("summary"):
        extra_checks.append(f"🔍 VIN: {vin_info['summary']}")
    if cadastr_info.get("summary"):
        extra_checks.append(f"🏛 Кадастр: {cadastr_info['summary']}")
    if competition:
        extra_checks.append(f"👥 {competition}")
    extra_str = "\n".join(extra_checks)

    disc = result.get("discount_pct", 0)
    # Пересчитываем дисконт от реальной цены если есть
    if market_price_real > 0 and lot_price > 0:
        disc = round((market_price_real - lot_price) / market_price_real * 100)

    # Источник рыночной цены
    market_source = market_data.get("data_source", "")
    listings = market_data.get("listings_count", 0)
    market_note = f" _{market_source}, {listings} объявл._" if market_source and listings > 0 else ""

    return {
        "lot_type":         lot_type,
        "total_score":      result.get("total_score", 5.0),
        "price":            fmt(lot_price),
        "market_price":     fmt(market_price_real or result.get("market_price_rub", 0)),
        "market_note":      market_note,
        "discount_pct":     str(disc) if disc else "?",
        "step":             step_info,
        "participants":     participants,
        "competition":      competition,
        "liquidity_text":   f"{result.get('liquidity_level','средняя')} (~{result.get('liquidity_days',90)} дней)",
        "roi_text":         invest["summary"],
        "legal_text":       legal_text,
        "owners":           result.get("owners_count", "?"),
        "encumbrances":     result.get("encumbrances", "нет данных"),
        "extra_checks":     extra_str,
        "risk_level":       result.get("risk_level", "средний"),
        "invest_potential": result.get("invest_potential", "средний"),
        "strategy":         result.get("strategy", ""),
        "what_to_check":    result.get("what_to_check", ""),
        "action":           result.get("action", "ПРОВЕРИТЬ ДОКУМЕНТЫ"),
        "action_emoji":     result.get("action_emoji", "⚠️"),
        "verdict":          result.get("verdict", ""),
        "worth_showing":    is_worth_showing(lot_type, float(result.get("total_score", 0))),
        "vin":              vin,
        "cadastral":        cadastral,
    }
