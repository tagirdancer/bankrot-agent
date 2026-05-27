"""
Анализатор v4.0 — глубокий инвестиционный анализ
"""
import httpx, json, re, os, asyncio
from dotenv import load_dotenv
load_dotenv()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = os.getenv("GROQ_API_KEY")
MODEL    = "llama-3.1-8b-instant"

# Минимальный балл для показа по категории
MIN_SCORE = {
    "квартира":  0,    # всегда показываем
    "дом":       0,    # всегда показываем
    "коммерция": 0,    # всегда показываем
    "земля":     7.0,  # только хорошие
    "авто":      8.0,  # только отличные
    "гараж":     8.0,  # только отличные
    "бизнес":    8.0,  # только отличные
    "прочее":    8.5,  # только исключительные
}


def detect_type(text: str) -> str:
    t = text.lower()
    auto_words = [
        "автомобил", "легков", "грузов", "седан", "хэтчбек", "внедорожник",
        "кроссовер", "минивэн", "микроавтобус", "автобус", "мотоцикл",
        "прицеп", "полуприцеп", "спецтехник", "экскаватор", "трактор",
        "бульдозер", "кран", "погрузчик", "самосвал", "камаз", "газель",
        "уаз", "ваз", "lada", "bmw", "mercedes", "toyota", "hyundai",
        "kia", "volkswagen", "ford", "renault", "nissan", "mazda", "honda",
        "audi", "volvo", "skoda", "opel", "тойота", "хендай", "киа",
        "фольксваген", "транспортн", "двигател",
    ]
    if any(w in t for w in auto_words):
        return "авто"
    if any(w in t for w in ["гараж", "машиноместо", "парковочн"]):
        return "гараж"
    land_words = ["земельн", "участок", "га ", "гектар", "снт ", "днп ",
                  "ижс", "лпх ", "сельхоз", "пашн", "угодь", "садовод"]
    if any(w in t for w in land_words):
        if not any(w in t for w in ["квартир", "комнат", "студи"]):
            return "земля"
    flat_words = ["квартир", "комнат", "студи", "апартамент",
                  "однокомнат", "двухкомнат", "1-комн", "2-комн", "3-комн",
                  "жилое помещение"]
    if any(w in t for w in flat_words):
        return "квартира"
    house_words = ["жилой дом", "дача", "коттедж", "таунхаус",
                   "садовый дом", "часть дома", "домовлад", "загородн"]
    if any(w in t for w in house_words):
        return "дом"
    commercial_words = ["нежилое", "офис", "торгов", "магазин", "склад",
                        "производ", "ресторан", "кафе", "гостиниц", "здание",
                        "помещени", "псн", "арендн", "павильон", "цех", "ангар"]
    if any(w in t for w in commercial_words):
        return "коммерция"
    biz_words = ["оборудован", "станок", "доля в ооо", "акци",
                 "дебиторск", "право требован"]
    if any(w in t for w in biz_words):
        return "бизнес"
    return "прочее"


def is_worth_showing(lot_type: str, score: float) -> bool:
    """Показывать ли лот в отчёте"""
    min_s = MIN_SCORE.get(lot_type, 8.0)
    return score >= min_s


async def get_lot_details(lot_url: str, page) -> dict:
    details = {"price": 0, "title_full": "", "description": "",
               "step_current": 0, "step_total": 0}
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
            r'цена[^\d]*(\d[\d\s]{3,})',
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
        step_match = re.search(
            r'(\d+)\s*/\s*(\d+)|шаг[^\d]*(\d+)[^\d]+(\d+)',
            full_text, re.IGNORECASE
        )
        if step_match:
            g = step_match.groups()
            try:
                cur = int(g[0] or g[2] or 0)
                tot = int(g[1] or g[3] or 0)
                if 0 < cur <= tot <= 20:
                    details["step_current"] = cur
                    details["step_total"] = tot
            except:
                pass
    except:
        pass
    return details


async def call_groq(prompt: str, max_tokens: int = 700) -> str:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": MODEL,
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
    title     = lot.get("title_full") or lot.get("title", "")
    region    = lot.get("region", "moskva")
    pdf_text  = lot.get("pdf_text", "")
    lot_info  = lot.get("description", "")
    lot_price = lot.get("price", 0)
    lot_type  = lot.get("category", "прочее")
    step_cur  = lot.get("step_current", 0)
    step_tot  = lot.get("step_total", 0)
    region_name = "Москва" if "moskva" in region else "Московская область"

    if lot_price == 0:
        for pattern in [r'(\d[\d\s]{4,})\s*(?:руб|₽)',
                        r'цена[^\d]*(\d[\d\s]{4,})']:
            m = re.search(pattern, pdf_text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s', '', m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        lot_price = p
                        break
                except:
                    pass

    step_info = ""
    steps_left = 0
    if step_cur and step_tot:
        steps_left = step_tot - step_cur
        step_info = f"Шаг торгов: {step_cur}/{step_tot} (осталось {steps_left} снижений)"

    prompt = f"""Ты топ-эксперт по инвестициям в банкротные торги России. 
Дай глубокий профессиональный анализ как для состоятельного инвестора.

═══ ОБЪЕКТ ═══
Тип: {lot_type}
Название: {title[:200]}
Регион: {region_name}
Цена на торгах: {f'{lot_price:,.0f} руб'.replace(',', ' ') if lot_price > 0 else 'не определена'}
{step_info}

═══ ДАННЫЕ ЕГРН ═══
{pdf_text[:700]}

═══ ОПИСАНИЕ С ТОРГОВ ═══
{lot_info[:500]}

Проведи полный инвестиционный анализ. Ответь ТОЛЬКО JSON:
{{
  "total_score": 8.2,
  "market_price_rub": 12000000,
  "discount_pct": 42,
  "price_per_sqm_market": 180000,
  "liquidity_level": "высокая",
  "liquidity_days": 30,
  
  "legal_score": 9,
  "owners_count": "1",
  "encumbrances": "ипотека Сбербанк — снимается автоматически при покупке",
  "red_flags": [],
  "legal_summary": "1 собственник с 2019г, ипотека Сбербанка снимается, судебных дел нет",
  
  "roi_rent_years": 11,
  "roi_rent_monthly": 55000,
  "roi_flip_pct": 42,
  "roi_flip_rub": 5000000,
  "roi_text": "Аренда: ~55 000 руб/мес, окупаемость 11 лет. Перепродажа: +5 млн руб (+42%)",
  
  "action": "ВХОДИТЬ СЕЙЧАС",
  "action_emoji": "🟢",
  "strategy": "Цена на 42% ниже рынка — лучшее предложение в категории за месяц. Залог Сбербанка даёт юридическую чистоту. Квартиры в этом районе уходят за 30 дней. Рекомендую подать заявку сегодня.",
  "what_to_check": "Запросить у АУ документы о задолженности по ЖКХ. Осмотреть квартиру лично перед подачей заявки.",
  "invest_potential": "высокий",
  "risk_level": "низкий",
  "verdict": "РЕКОМЕНДУЕТСЯ К ПОКУПКЕ"
}}

action: ВХОДИТЬ СЕЙЧАС / ЖДАТЬ СНИЖЕНИЯ / ПРОВЕРИТЬ ДОКУМЕНТЫ / ПРОПУСТИТЬ
invest_potential: высокий / средний / низкий  
risk_level: низкий / средний / высокий / критический

Будь конкретным в strategy — почему именно сейчас или ждать, что проверить."""

    raw = await call_groq(prompt, 700)

    try:
        m = re.search(r'\{[\s\S]*\}', raw)
        result = json.loads(m.group()) if m else {}
    except:
        result = {}

    def fmt(p):
        try:
            p = float(p)
            if p >= 1_000_000:
                return f"{p/1_000_000:.1f} млн ₽"
            elif p > 0:
                return f"{int(p):,} ₽".replace(',', ' ')
        except:
            pass
        return "уточните на сайте"

    red_flags = result.get("red_flags", [])
    legal_text = result.get("legal_summary", "требует проверки")
    if red_flags:
        legal_text += " ⚠️ " + "; ".join(str(f) for f in red_flags[:2])

    disc    = result.get("discount_pct", 0)
    score   = float(result.get("total_score", 5.0))
    action  = result.get("action", "ПРОВЕРИТЬ ДОКУМЕНТЫ")
    step_str = f"Шаг {step_cur}/{step_tot} (осталось {steps_left})" if step_cur else ""

    return {
        "lot_type":        lot_type,
        "total_score":     score,
        "price":           fmt(lot_price),
        "market_price":    fmt(result.get("market_price_rub", 0)),
        "discount_pct":    str(disc) if disc else "?",
        "step":            step_str,
        "liquidity_text":  f"{result.get('liquidity_level','средняя')} (~{result.get('liquidity_days',90)} дней)",
        "roi_text":        result.get("roi_text", "нет данных"),
        "legal_text":      legal_text,
        "owners":          result.get("owners_count", "?"),
        "encumbrances":    result.get("encumbrances", "нет данных"),
        "risk_level":      result.get("risk_level", "средний"),
        "invest_potential":result.get("invest_potential", "средний"),
        "strategy":        result.get("strategy", "Требует дополнительного анализа."),
        "what_to_check":   result.get("what_to_check", ""),
        "action":          action,
        "action_emoji":    result.get("action_emoji", "⚠️"),
        "verdict":         result.get("verdict", action),
        "worth_showing":   is_worth_showing(lot_type, score),
    }
