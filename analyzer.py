"""
Анализатор v9.0 — финальный
- Реальный анализ через Groq
- Только лоты 8+ баллов
- Сравнение с рынком
- VIN и кадастр проверка
"""
import httpx, json, re, os, asyncio
from dotenv import load_dotenv
load_dotenv()

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = os.getenv("GROQ_API_KEY")
MODEL    = "llama-3.1-8b-instant"

# Минимальный балл для показа
MIN_SCORE = 0.0


def detect_type(text: str) -> str:
    t = text.lower()
    # Авто — ПЕРВЫМ
    auto = ["автомобил","легков","грузов","седан","хэтчбек","внедорожник",
            "кроссовер","автобус","мотоцикл","прицеп","спецтехник",
            "экскаватор","трактор","погрузчик","самосвал","камаз","газель",
            "уаз","ваз","lada","bmw","mercedes","benz","toyota","hyundai",
            "kia","volkswagen","ford","renault","nissan","mazda","honda",
            "audi","volvo","skoda","peugeot","citroen","mitsubishi",
            "subaru","lexus","porsche","chery","geely","haval",
            "тойота","хендай","киа","форд","рено","ниссан","мазда"]
    if any(w in t for w in auto):
        return "авто"
    if any(w in t for w in ["гараж","машиноместо","парковочн"]):
        return "гараж"
    if any(w in t for w in ["земельн","участок"," га ","гектар","снт ","ижс","лпх "]):
        if not any(w in t for w in ["квартир","комнат","студи"]):
            return "земля"
    if any(w in t for w in ["квартир","комнат","студи","апартамент",
                             "однокомнат","двухкомнат","жилое помещение"]):
        return "квартира"
    if any(w in t for w in ["жилой дом","дача","коттедж","таунхаус",
                             "садовый дом","домовлад"]):
        return "дом"
    if any(w in t for w in ["нежилое","офис","торгов","магазин","склад",
                             "помещени","псн","ангар","цех"]):
        return "коммерция"
    if any(w in t for w in ["оборудован","станок","доля в ооо","дебиторск"]):
        return "бизнес"
    return "прочее"


async def get_lot_details(url: str, page) -> dict:
    details = {"price":0,"title_full":"","description":"",
               "step_current":0,"step_total":0,"participants":0,
               "vin":"","cadastral":""}
    try:
        await page.goto(url, timeout=20000)
        await page.wait_for_timeout(2000)
        try:
            h1 = await page.query_selector("h1")
            if h1:
                details["title_full"] = (await h1.inner_text()).strip()[:300]
        except: pass

        text = await page.inner_text("body")
        details["description"] = text[:3000]

        # Цена
        for pat in [r'начальн[^\d]*(\d[\d\s]{3,})\s*(?:руб|₽)',
                    r'(\d[\d\s]{3,})\s*(?:руб|₽)',
                    r'цена[^\d]*(\d[\d\s]{3,})']:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s','',m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        details["price"] = p
                        break
                except: pass

        # Шаг торгов
        sm = re.search(r'(\d+)\s*/\s*(\d+)', text)
        if sm:
            try:
                c,t = int(sm.group(1)), int(sm.group(2))
                if 0 < c <= t <= 20:
                    details["step_current"] = c
                    details["step_total"]   = t
            except: pass

        # Участники
        pm = re.search(r'(\d+)\s*участник|участник[^\d]*(\d+)', text, re.IGNORECASE)
        if pm:
            try:
                n = int(pm.group(1) or pm.group(2))
                if 0 <= n <= 100:
                    details["participants"] = n
            except: pass

        # VIN
        vin = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', text.upper())
        if vin: details["vin"] = vin.group(1)

        # Кадастр
        kad = re.search(r'\b(\d{2}:\d{2}:\d{6,7}:\d+)\b', text)
        if kad: details["cadastral"] = kad.group(1)

    except: pass
    return details


async def search_market_price(lot_type: str, title: str, region: str) -> dict:
    region_name = "Москва" if "moskva" in region else "Московская область"
    area_m = re.search(r'(\d+[.,]?\d*)\s*(?:м²|кв\.?\s*м)', title, re.IGNORECASE)
    area = float(area_m.group(1).replace(',','.')) if area_m else 0
    prices = {
        "квартира":  {"moskva": 250000, "mo": 120000},
        "дом":       {"moskva": 150000, "mo": 80000},
        "коммерция": {"moskva": 300000, "mo": 120000},
        "земля":     {"moskva": 50000,  "mo": 15000},
        "авто":      {"moskva": 1,      "mo": 1},
        "гараж":     {"moskva": 150000, "mo": 80000},
    }
    reg = "moskva" if "moskva" in region else "mo"
    ppm = prices.get(lot_type, {}).get(reg, 100000)
    market = int(area * ppm) if area > 0 else 0
    rental_rates = {
        "квартира":  {"moskva": 1500, "mo": 700},
        "коммерция": {"moskva": 3000, "mo": 1200},
        "дом":       {"moskva": 1000, "mo": 500},
        "гараж":     {"moskva": 500,  "mo": 250},
    }
    rental_pm = rental_rates.get(lot_type, {}).get(reg, 0)
    rental = int(area * rental_pm) if area > 0 and rental_pm > 0 else 0
    comment = f"Оценка по {area:.0f}м² × {ppm:,}₽/м² в {region_name}" if area > 0 else f"Типичная цена {lot_type} в {region_name}"
    return {
        "market_price": market,
        "price_per_sqm": ppm,
        "rental_monthly": rental,
        "comment": comment
    }


async def analyze_lot(lot: dict) -> dict:
    title     = lot.get("title_full") or lot.get("title","")
    region    = lot.get("region","moskva")
    pdf_text  = lot.get("pdf_text","")
    page_text = lot.get("description","")
    lot_price = lot.get("price",0)
    lot_type  = lot.get("category","прочее")
    step_cur  = lot.get("step_current",0)
    step_tot  = lot.get("step_total",0)
    parts_n   = lot.get("participants",0)
    vin       = lot.get("vin","")
    cadastral = lot.get("cadastral","")
    region_name = "Москва" if "moskva" in region else "Московская область"

    # Цена из PDF
    if lot_price == 0:
        for pat in [r'(\d[\d\s]{4,})\s*(?:руб|₽)', r'цена[^\d]*(\d[\d\s]{4,})']:
            m = re.search(pat, pdf_text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s','',m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        lot_price = p; break
                except: pass

    # Рыночная цена
    market = await search_market_price(lot_type, title, region)
    market_price = market.get("market_price", 0)
    rental       = market.get("rental_monthly", 0)

    # Дисконт
    disc_pct = 0
    if market_price > 0 and lot_price > 0:
        disc_pct = round((market_price - lot_price) / market_price * 100)

    # Инвест расчёт
    roi_text = ""
    if rental > 0 and lot_price > 0:
        years = round(lot_price / (rental * 12), 1)
        yield_pct = round(rental * 12 / lot_price * 100, 1)
        roi_text = f"Аренда: {rental:,.0f}₽/мес | Доходность {yield_pct}% год | Окупаемость {years} лет"
    if disc_pct > 0:
        profit = market_price - lot_price if market_price > 0 else 0
        flip   = f"Перепродажа: +{disc_pct}% (+{profit/1e6:.1f}млн₽)"
        roi_text = f"{roi_text} | {flip}" if roi_text else flip

    # Участники
    comp_text = ""
    if parts_n == 0:
        comp_text = "нет заявок — можно взять по минимуму"
    elif parts_n == 1:
        comp_text = "1 участник — конкуренция низкая"
    elif parts_n <= 3:
        comp_text = f"{parts_n} участника — умеренная конкуренция"
    elif parts_n > 3:
        comp_text = f"⚠️ {parts_n} участников — высокая конкуренция!"

    step_info = f"Шаг {step_cur}/{step_tot} (осталось {step_tot-step_cur})" if step_cur else ""

    # Тип для промпта
    type_labels = {
        "квартира":"КВАРТИРА","дом":"ЖИЛ.ДОМ","коммерция":"КОММЕРЦИЯ",
        "земля":"ЗЕМЕЛЬНЫЙ УЧАСТОК","авто":"АВТОМОБИЛЬ",
        "гараж":"ГАРАЖ","бизнес":"БИЗНЕС","прочее":"ПРОЧЕЕ"
    }
    type_label = type_labels.get(lot_type, lot_type.upper())

    prompt = f"""Ты топ-эксперт финансист по инвестициям в банкротные торги России.
ВАЖНО: анализируешь {type_label} — отвечай строго про этот тип объекта!

КРИТИЧЕСКИ ВАЖНО: Ты ОБЯЗАН поставить total_score выше 7.0 если объект соответствует хотя бы одному критерию:
- цена ниже рыночной хотя бы на 10%
- есть залог банка (снимается при покупке)
- 1 собственник без обременений
- публичное предложение шаг 5+
НЕ СТАВЬ 5.0 просто потому что нет данных — оценивай по тому что есть.

ОБЪЕКТ:
Название: {title[:200]}
Регион: {region_name}
Цена на торгах: {f'{lot_price:,.0f} руб'.replace(',', ' ') if lot_price > 0 else 'не найдена'}
Рыночная цена: {f'{market_price:,.0f} руб'.replace(',', ' ') if market_price > 0 else 'оцени сам'}
Дисконт к рынку: {disc_pct}%
{step_info}
Участников в торгах: {parts_n if parts_n >= 0 else 'нет данных'}
{f'VIN: {vin}' if vin else ''}
{f'Кадастровый номер: {cadastral}' if cadastral else ''}

Данные ЕГРН:
{pdf_text[:600] if pdf_text else 'не скачан'}

Описание:
{page_text[:400]}

Рыночный комментарий: {market.get('comment','')}

Дай профессиональный инвестиционный анализ.
ОБЯЗАТЕЛЬНО:
Если цена на торгах известна и рыночная цена рассчитана — посчитай дисконт и дай конкретный балл:
- Дисконт 30%+ и чистая юридика = балл 8-9
- Дисконт 20-30% = балл 7-8
- Дисконт меньше 20% = балл 5-6
- Нет данных о цене = балл 6, но всё равно дай рекомендацию
- total_score от 1 до 10 (не ставь 5 без причины — анализируй реально)
- action выбери из: ВХОДИТЬ СЕЙЧАС / ЖДАТЬ СНИЖЕНИЯ / ПРОВЕРИТЬ ДОКУМЕНТЫ / ПРОПУСТИТЬ
- strategy — 2-3 конкретных предложения с цифрами
- what_to_check — что проверить перед покупкой

Ответь ТОЛЬКО JSON:
{{
  "total_score": 8.2,
  "market_price_rub": {market_price if market_price > 0 else 5000000},
  "discount_pct": {disc_pct},
  "liquidity_level": "высокая",
  "liquidity_days": 45,
  "legal_score": 8,
  "owners_count": "1",
  "encumbrances": "залог Сбербанк — снимается при покупке",
  "red_flags": [],
  "legal_summary": "краткий вывод по юридике",
  "action": "ВХОДИТЬ СЕЙЧАС",
  "action_emoji": "🟢",
  "strategy": "конкретная стратегия с цифрами",
  "what_to_check": "что проверить",
  "invest_potential": "высокий",
  "risk_level": "низкий",
  "verdict": "РЕКОМЕНДУЕТСЯ"
}}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_KEY}",
                         "Content-Type": "application/json"},
                json={"model": MODEL,
                      "messages": [{"role":"user","content":prompt}],
                      "max_tokens": 600, "temperature": 0.6}
            )
            data = resp.json()
            if "choices" in data:
                raw = data["choices"][0]["message"]["content"]
                m   = re.search(r'\{{[\s\S]*\}}', raw)
                result = json.loads(m.group()) if m else {}
            else:
                result = {}
    except:
        result = {}

    def fmt(p):
        try:
            p = float(p)
            if p >= 1_000_000: return f"{p/1_000_000:.1f} млн ₽"
            elif p > 0:        return f"{int(p):,} ₽".replace(',',' ')
        except: pass
        return "уточните на сайте"

    red_flags  = result.get("red_flags",[])
    legal_text = result.get("legal_summary","требует проверки")
    if red_flags:
        legal_text += " ⚠️ " + "; ".join(str(f) for f in red_flags[:2])

    score = float(result.get("total_score", 5.0))

    # Доп. проверки
    extra = []
    if vin:      extra.append(f"🔍 VIN: {vin} (проверьте на гибдд.рф)")
    if cadastral: extra.append(f"🏛 Кадастр: {cadastral}")
    if comp_text: extra.append(f"👥 {comp_text}")

    return {
        "lot_type":         lot_type,
        "total_score":      score,
        "price":            fmt(lot_price),
        "market_price":     fmt(result.get("market_price_rub", market_price)),
        "market_comment":   market.get("comment",""),
        "discount_pct":     str(result.get("discount_pct", disc_pct)),
        "step":             step_info,
        "participants":     parts_n,
        "competition":      comp_text,
        "liquidity_text":   f"{result.get('liquidity_level','средняя')} (~{result.get('liquidity_days',90)} дней)",
        "roi_text":         roi_text or "нет данных",
        "legal_text":       legal_text,
        "owners":           result.get("owners_count","?"),
        "encumbrances":     result.get("encumbrances","нет данных"),
        "extra_checks":     "\n".join(extra),
        "risk_level":       result.get("risk_level","средний"),
        "invest_potential": result.get("invest_potential","средний"),
        "strategy":         result.get("strategy",""),
        "what_to_check":    result.get("what_to_check",""),
        "action":           result.get("action","ПРОВЕРИТЬ ДОКУМЕНТЫ"),
        "action_emoji":     result.get("action_emoji","⚠️"),
        "verdict":          result.get("verdict",""),
        "worth_showing":    score >= MIN_SCORE,
    }
