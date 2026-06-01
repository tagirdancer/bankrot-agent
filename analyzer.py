"""
Анализатор v10.0 — финальный с умным баллом
"""
import httpx, json, re, os, asyncio
from dotenv import load_dotenv
load_dotenv()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = os.getenv("GROQ_API_KEY")
MODEL    = "llama-3.1-8b-instant"
MIN_SCORE = 0.0

def detect_type(text: str) -> str:
    t = text.lower()
    auto = ["автомобил","легков","грузов","седан","хэтчбек","внедорожник",
            "кроссовер","автобус","мотоцикл","прицеп","спецтехник",
            "экскаватор","трактор","погрузчик","самосвал","камаз","газель",
            "уаз","ваз","lada","bmw","mercedes","benz","toyota","hyundai",
            "kia","volkswagen","ford","renault","nissan","mazda","honda",
            "audi","volvo","skoda","peugeot","citroen","mitsubishi",
            "subaru","lexus","porsche","chery","geely","haval",
            "тойота","хендай","киа","форд","рено","ниссан","мазда"]
    if any(w in t for w in auto): return "авто"
    if any(w in t for w in ["гараж","машиноместо","парковочн"]): return "гараж"
    if any(w in t for w in ["земельн","участок"," га ","гектар","снт ","ижс","лпх "]):
        if not any(w in t for w in ["квартир","комнат","студи"]): return "земля"
    if any(w in t for w in ["квартир","комнат","студи","апартамент",
                             "однокомнат","двухкомнат","жилое помещение"]): return "квартира"
    if any(w in t for w in ["жилой дом","дача","коттедж","таунхаус",
                             "садовый дом","домовлад"]): return "дом"
    if any(w in t for w in ["нежилое","офис","торгов","магазин","склад",
                             "помещени","псн","ангар","цех"]): return "коммерция"
    if any(w in t for w in ["оборудован","станок","доля в ооо","дебиторск"]): return "бизнес"
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
            if h1: details["title_full"] = (await h1.inner_text()).strip()[:300]
        except: pass
        text = await page.inner_text("body")
        details["description"] = text[:3000]
        for pat in [r'начальн[^\d]*(\d[\d\s]{3,})\s*(?:руб|₽)',
                    r'(\d[\d\s]{3,})\s*(?:руб|₽)',
                    r'цена[^\d]*(\d[\d\s]{3,})']:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s','',m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        details["price"] = p; break
                except: pass
        sm = re.search(r'(\d+)\s*/\s*(\d+)', text)
        if sm:
            try:
                c,t = int(sm.group(1)), int(sm.group(2))
                if 0 < c <= t <= 20:
                    details["step_current"] = c
                    details["step_total"] = t
            except: pass
        pm = re.search(r'(\d+)\s*участник|заявок[^\d]*(\d+)', text, re.IGNORECASE)
        if pm:
            try:
                n = int(pm.group(1) or pm.group(2) or 0)
                if 0 <= n <= 100: details["participants"] = n
            except: pass
        vin = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', text.upper())
        if vin: details["vin"] = vin.group(1)
        kad = re.search(r'\b(\d{2}:\d{2}:\d{6,7}:\d+)\b', text)
        if kad: details["cadastral"] = kad.group(1)
    except: pass
    return details


def calc_market_price(lot_type: str, title: str, region: str) -> dict:
    area_m = re.search(r'(\d+[.,]?\d*)\s*(?:м²|кв\.?\s*м)', title, re.IGNORECASE)
    area = float(area_m.group(1).replace(',','.')) if area_m else 0
    reg = "moskva" if "moskva" in region else "mo"
    region_name = "Москва" if reg == "moskva" else "Московская область"
    prices = {
        "квартира":  {"moskva":260000,"mo":130000},
        "дом":       {"moskva":160000,"mo":85000},
        "коммерция": {"moskva":320000,"mo":130000},
        "земля":     {"moskva":55000, "mo":18000},
        "авто":      {"moskva":1,     "mo":1},
        "гараж":     {"moskva":160000,"mo":90000},
    }
    rentals = {
        "квартира":  {"moskva":1600,"mo":750},
        "коммерция": {"moskva":3200,"mo":1300},
        "дом":       {"moskva":1100,"mo":550},
        "гараж":     {"moskva":550, "mo":280},
    }
    ppm    = prices.get(lot_type,{}).get(reg, 100000)
    rpm    = rentals.get(lot_type,{}).get(reg, 0)
    market = int(area * ppm) if area > 0 else 0
    rental = int(area * rpm) if area > 0 and rpm > 0 else 0
    comment = f"{area:.0f}м² × {ppm:,}₽/м² в {region_name}" if area > 0 else f"типичная цена в {region_name}"
    return {"market_price":market,"rental_monthly":rental,
            "price_per_sqm":ppm,"comment":comment,"area":area}


def calc_score(lot_price, market_price, rental, parts_n,
               cadastral, step_cur, step_tot) -> float:
    score = 5.0
    disc = 0
    if market_price > 0 and lot_price > 0 and market_price > lot_price:
        disc = (market_price - lot_price) / market_price * 100
    if disc >= 40: score += 3.0
    elif disc >= 30: score += 2.5
    elif disc >= 20: score += 1.5
    elif disc >= 10: score += 0.8
    if rental > 0 and lot_price > 0:
        yld = rental * 12 / lot_price * 100
        if yld >= 12: score += 1.5
        elif yld >= 9:  score += 1.2
        elif yld >= 7:  score += 0.8
        elif yld >= 5:  score += 0.4
    if parts_n == 0:   score += 0.5
    elif parts_n <= 2: score += 0.2
    elif parts_n > 5:  score -= 0.5
    if cadastral: score += 0.3
    if step_cur and step_tot:
        left = step_tot - step_cur
        if left <= 1: score += 0.7
        elif left <= 3: score += 0.4
    return round(min(10.0, max(1.0, score)), 1)


async def get_expert_analysis(title, lot_type, region_name, lot_price,
                               market_price, disc_pct, rental, parts_n,
                               step_info, cadastral, pdf_text, score) -> dict:
    action = "ПРОВЕРИТЬ ДОКУМЕНТЫ"
    if score >= 8.0:   action = "ВХОДИТЬ СЕЙЧАС"
    elif score >= 7.0: action = "ЖДАТЬ СНИЖЕНИЯ"
    elif score < 5.0:  action = "ПРОПУСТИТЬ"

    prompt = f"""Ты финансовый эксперт по инвестициям в банкротные торги России.
Анализируй строго как {lot_type.upper()}.
ДАННЫЕ:
Объект: {title[:150]}
Регион: {region_name}
Цена торгов: {f'{lot_price:,.0f}₽'.replace(',','') if lot_price else 'неизвестна'}
Рыночная цена: {f'{market_price:,.0f}₽'.replace(',','') if market_price else 'неизвестна'}
Дисконт: {disc_pct}%
Участников: {parts_n}
Аренда/мес: {f'{rental:,.0f}₽'.replace(',','') if rental else 'нет данных'}
{step_info}
{f'Кадастр: {cadastral}' if cadastral else ''}
Данные объекта: {pdf_text[:500] if pdf_text else 'не получены'}
Дай краткий экспертный вывод. Отвечай ТОЛЬКО JSON:
{{
  "legal_summary": "1-2 предложения о юридике объекта",
  "strategy": "2-3 конкретных предложения: почему входить или нет, что важно проверить, какой потенциал",
  "what_to_check": "список через запятую: что проверить перед покупкой",
  "invest_potential": "высокий",
  "risk_level": "низкий",
  "liquidity_level": "высокая",
  "liquidity_days": 45,
  "verdict": "РЕКОМЕНДУЕТСЯ"
}}"""

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization":f"Bearer {GROQ_KEY}",
                         "Content-Type":"application/json"},
                json={"model":MODEL,
                      "messages":[{"role":"user","content":prompt}],
                      "max_tokens":400,"temperature":0.6}
            )
            data = resp.json()
            if "choices" in data:
                raw = data["choices"][0]["message"]["content"]
                m   = re.search(r'\{{[\s\S]*\}}', raw)
                if m: return json.loads(m.group())
    except: pass
    return {
        "legal_summary": "требует ручной проверки документов",
        "strategy": f"Дисконт {disc_pct}% к рынку. {'Нет заявок — можно взять по минимуму.' if parts_n == 0 else f'{parts_n} участников — есть конкуренция.'} Проверьте документы перед подачей заявки.",
        "what_to_check": "выписка ЕГРН, задолженность ЖКХ, прописанные лица",
        "invest_potential": "средний",
        "risk_level": "средний",
        "liquidity_level": "средняя",
        "liquidity_days": 90,
        "verdict": "ПРОВЕРИТЬ ДОКУМЕНТЫ"
    }


async def analyze_lot(lot: dict) -> dict:
    title     = lot.get("title_full") or lot.get("title","")
    region    = lot.get("region","moskva")
    pdf_text  = lot.get("pdf_text","") or lot.get("description","")[:1000]
    lot_price = lot.get("price",0)
    lot_type  = lot.get("category","прочее")
    step_cur  = lot.get("step_current",0)
    step_tot  = lot.get("step_total",0)
    parts_n   = lot.get("participants",0)
    vin       = lot.get("vin","")
    cadastral = lot.get("cadastral","")
    region_name = "Москва" if "moskva" in region else "Московская область"

    if lot_price == 0:
        for pat in [r'(\d[\d\s]{4,})\s*(?:руб|₽)',r'цена[^\d]*(\d[\d\s]{4,})']:
            m = re.search(pat, pdf_text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s','',m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        lot_price = p; break
                except: pass

    market    = calc_market_price(lot_type, title, region)
    mkt_price = market["market_price"]
    rental    = market["rental_monthly"]

    disc_pct = 0
    if mkt_price > 0 and lot_price > 0 and mkt_price > lot_price:
        disc_pct = round((mkt_price - lot_price) / mkt_price * 100)

    score     = calc_score(lot_price, mkt_price, rental, parts_n,
                           cadastral, step_cur, step_tot)
    step_info = f"Шаг {step_cur}/{step_tot} (осталось {step_tot-step_cur})" if step_cur else ""

    expert = await get_expert_analysis(
        title, lot_type, region_name, lot_price, mkt_price,
        disc_pct, rental, parts_n, step_info, cadastral, pdf_text, score
    )

    roi_parts = []
    if rental > 0 and lot_price > 0:
        yld   = round(rental * 12 / lot_price * 100, 1)
        years = round(lot_price / (rental * 12), 1)
        roi_parts.append(f"Аренда {rental:,}₽/мес | {yld}% год | {years} лет")
    if disc_pct > 0 and mkt_price > 0 and lot_price > 0:
        profit = mkt_price - lot_price
        roi_parts.append(f"Перепродажа +{disc_pct}% (+{profit/1e6:.1f}млн₽)")
    roi_text = " | ".join(roi_parts) if roi_parts else "нет данных"

    action_map = {
        "ВХОДИТЬ СЕЙЧАС":      "🟢",
        "ЖДАТЬ СНИЖЕНИЯ":      "⏳",
        "ПРОВЕРИТЬ ДОКУМЕНТЫ": "⚠️",
        "ПРОПУСТИТЬ":          "🔴",
    }
    action = "ВХОДИТЬ СЕЙЧАС" if score >= 8 else \
             "ЖДАТЬ СНИЖЕНИЯ" if score >= 7 else \
             "ПРОВЕРИТЬ ДОКУМЕНТЫ" if score >= 5 else "ПРОПУСТИТЬ"

    extra = []
    if vin:       extra.append(f"🔍 VIN: {vin}")
    if cadastral: extra.append(f"🏛 Кадастр: {cadastral}")
    if parts_n == 0:      extra.append("👥 Нет заявок — можно взять по минимуму")
    elif parts_n <= 2:    extra.append(f"👥 {parts_n} участника — конкуренция низкая")
    elif parts_n > 5:     extra.append(f"👥 ⚠️ {parts_n} участников — высокая конкуренция!")
    else:                 extra.append(f"👥 {parts_n} участника")

    def fmt(p):
        try:
            p = float(p)
            if p >= 1_000_000: return f"{p/1_000_000:.1f} млн ₽"
            elif p > 0:        return f"{int(p):,} ₽".replace(',',' ')
        except: pass
        return "уточните на сайте"

    invest = {"высокий":"🔥","средний":"📈","низкий":"📉"}
    risk   = {"низкий":"🟢","средний":"🟡","высокий":"🟠","критический":"🔴"}
    ip     = expert.get("invest_potential","средний")
    rl     = expert.get("risk_level","средний")

    return {
        "lot_type":         lot_type,
        "total_score":      score,
        "score_label":      f"{'🔥' if score>=9 else '⭐' if score>=8 else '📊'} {score}/10",
        "price":            fmt(lot_price),
        "market_price":     fmt(mkt_price),
        "market_comment":   market.get("comment",""),
        "discount_pct":     str(disc_pct) if disc_pct > 0 else "0",
        "step":             step_info,
        "liquidity_text":   f"{expert.get('liquidity_level','средняя')} (~{expert.get('liquidity_days',90)} дней)",
        "roi_text":         roi_text,
        "legal_text":       expert.get("legal_summary","требует проверки"),
        "extra_checks":     "\n".join(extra),
        "risk_text":        f"{risk.get(rl,'🟡')} риск: {rl}",
        "invest_text":      f"{invest.get(ip,'📈')} потенциал: {ip}",
        "strategy":         expert.get("strategy",""),
        "what_to_check":    expert.get("what_to_check",""),
        "action":           action,
        "action_emoji":     action_map.get(action,"⚠️"),
        "verdict":          expert.get("verdict",action),
        "worth_showing":    True,
    }
