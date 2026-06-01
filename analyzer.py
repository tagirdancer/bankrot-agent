"""
Анализатор v12.0
- Умный балл по формуле
- Полный юридический и инвестиционный анализ
- Реальные цены по м²
- VIN и кадастр проверка
- Без ошибок риск/потенциал
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
               "vin":"","cadastral":"","analytics_text":""}
    try:
        await page.goto(url, timeout=25000)
        await page.wait_for_timeout(2000)
        try:
            h1 = await page.query_selector("h1")
            if h1: details["title_full"] = (await h1.inner_text()).strip()[:300]
        except: pass
        text = await page.inner_text("body")
        details["description"] = text[:4000]
        for pat in [r'начальн[^\d]*(\d[\d\s]{3,})\s*(?:руб|₽)',
                    r'(\d[\d\s]{4,})\s*(?:руб|₽)',
                    r'цена[^\d]*(\d[\d\s]{3,})']:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s','',m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        details["price"] = p; break
                except: pass
        for pat in [r'шаг[^\d]*(\d+)[^\d]+(\d+)',r'(\d+)\s*/\s*(\d+)']:
            sm = re.search(pat, text, re.IGNORECASE)
            if sm:
                try:
                    c,t = int(sm.group(1)), int(sm.group(2))
                    if 0 < c <= t <= 20:
                        details["step_current"] = c
                        details["step_total"]   = t
                        break
                except: pass
        for pat in [r'заявок[^\d]*(\d+)',r'(\d+)\s*заявк',r'участник[^\d]*(\d+)']:
            pm = re.search(pat, text, re.IGNORECASE)
            if pm:
                try:
                    n = int(pm.group(1))
                    if 0 <= n <= 200: details["participants"] = n; break
                except: pass
        vin = re.search(r'\b([A-HJ-NPR-Z0-9]{17})\b', text.upper())
        if vin: details["vin"] = vin.group(1)
        kad = re.search(r'\b(\d{2}:\d{2}:\d{6,7}:\d+)\b', text)
        if kad: details["cadastral"] = kad.group(1)
        try:
            lot_id = re.search(r'id=(\d+)', url)
            if lot_id:
                lid = lot_id.group(1)
                await page.goto(f"https://tbankrot.ru/analytics/{lid}", timeout=15000)
                await page.wait_for_timeout(1500)
                analytics = await page.inner_text("body")
                if len(analytics) > 200:
                    details["analytics_text"] = analytics[:2000]
        except: pass
    except Exception as e:
        print(f"    details error: {e}")
    return details


def calc_market_price(lot_type: str, title: str, region: str) -> dict:
    area_m = re.search(r'(\d+[.,]?\d*)\s*(?:м²|кв\.?\s*м)', title, re.IGNORECASE)
    area   = float(area_m.group(1).replace(',','.')) if area_m else 0
    reg    = "moskva" if "moskva" in region else "mo"
    rname  = "Москва" if reg == "moskva" else "Московская область"
    prices  = {
        "квартира":  {"moskva":260000,"mo":130000},
        "дом":       {"moskva":200000,"mo":120000},
        "коммерция": {"moskva":320000,"mo":130000},
        "земля":     {"moskva":80000, "mo":25000},
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
    return {
        "market_price":   market,
        "rental_monthly": rental,
        "price_per_sqm":  ppm,
        "area":           area,
        "comment":        f"{area:.0f}м² × {ppm:,}₽/м² в {rname}" if area > 0 else f"типичная цена в {rname}"
    }


def calc_score(lot_price, market_price, rental, parts_n,
               cadastral, step_cur, step_tot) -> float:
    score = 5.0
    disc  = 0
    if market_price > 0 and lot_price > 0 and market_price > lot_price:
        disc = (market_price - lot_price) / market_price * 100
    if disc >= 50:   score += 3.5
    elif disc >= 40: score += 3.0
    elif disc >= 30: score += 2.5
    elif disc >= 20: score += 1.5
    elif disc >= 10: score += 0.8
    if rental > 0 and lot_price > 0:
        yld = rental * 12 / lot_price * 100
        if yld >= 12: score += 1.5
        elif yld >= 9:  score += 1.0
        elif yld >= 7:  score += 0.6
        elif yld >= 5:  score += 0.3
    if parts_n == 0:   score += 0.5
    elif parts_n <= 2: score += 0.2
    elif parts_n > 5:  score -= 0.5
    if cadastral: score += 0.3
    if step_cur and step_tot:
        left = step_tot - step_cur
        if left <= 1:   score += 0.7
        elif left <= 3: score += 0.4
    return round(min(10.0, max(1.0, score)), 1)


async def get_expert_analysis(title, lot_type, region_name, lot_price,
                               market_price, disc_pct, rental, parts_n,
                               step_info, cadastral, vin, pdf_text,
                               analytics_text, score) -> dict:
    all_docs = ""
    if pdf_text:       all_docs += f"\nЕГРН/документы:\n{pdf_text[:800]}"
    if analytics_text: all_docs += f"\nАналитика торгов:\n{analytics_text[:500]}"
    prompt = f"""Ты юрист и финансовый эксперт по банкротным торгам России.
Анализируй строго как {lot_type.upper()}.
ОБЪЕКТ: {title[:150]}
Регион: {region_name}
Цена: {f'{lot_price:,.0f}₽' if lot_price else 'не определена'}
Рынок: {f'{market_price:,.0f}₽' if market_price else 'не определена'}
Дисконт: {disc_pct}%
Участников: {parts_n}
Аренда/мес: {f'{rental:,.0f}₽' if rental else 'нет'}
{step_info}
{f'Кадастр: {cadastral}' if cadastral else ''}
{f'VIN: {vin}' if vin else ''}
{all_docs if all_docs else 'Документы: не получены'}
Ответь ТОЛЬКО JSON без лишнего текста:
{{
  "legal_summary": "кратко: собственники, обременения, залоги, аресты",
  "encumbrances": "ипотека Сбербанк снимается / арест / нет обременений",
  "owners_count": "1",
  "legal_risks": ["риск 1"],
  "invest_risks": ["инвест риск 1"],
  "invest_opportunities": ["возможность 1", "возможность 2"],
  "invest_potential": "высокий",
  "risk_level": "низкий",
  "liquidity_level": "высокая",
  "liquidity_days": 45,
  "exit_strategy": "перепродажа за 30-60 дней",
  "strategy": "конкретно 2-3 предложения почему входить или нет с цифрами",
  "what_to_check": "выписка ЕГРН, долги ЖКХ, прописанные лица",
  "action": "ВХОДИТЬ СЕЙЧАС",
  "verdict": "РЕКОМЕНДУЕТСЯ"
}}
action: ВХОДИТЬ СЕЙЧАС / ЖДАТЬ СНИЖЕНИЯ / ПРОВЕРИТЬ ДОКУМЕНТЫ / ПРОПУСТИТЬ
invest_potential: высокий / средний / низкий
risk_level: низкий / средний / высокий / критический"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                GROQ_URL,
                headers={"Authorization":f"Bearer {GROQ_KEY}",
                         "Content-Type":"application/json"},
                json={"model":MODEL,
                      "messages":[{"role":"user","content":prompt}],
                      "max_tokens":500,"temperature":0.5}
            )
            data = resp.json()
            if "choices" in data:
                raw = data["choices"][0]["message"]["content"]
                m   = re.search(r'\{{[\s\S]*\}}', raw)
                if m: return json.loads(m.group())
    except Exception as e:
        print(f"    Groq error: {e}")
    action = ("ВХОДИТЬ СЕЙЧАС" if score >= 8 else
              "ЖДАТЬ СНИЖЕНИЯ" if score >= 7 else
              "ПРОВЕРИТЬ ДОКУМЕНТЫ")
    opps = []
    if disc_pct > 0: opps.append(f"дисконт {disc_pct}% к рынку")
    if parts_n == 0: opps.append("нет конкурентов")
    return {
        "legal_summary":        "требует ручной проверки документов",
        "encumbrances":         "",
        "owners_count":         "?",
        "legal_risks":          ["нет данных"],
        "invest_risks":         ["требует проверки"],
        "invest_opportunities": opps or ["требует анализа"],
        "invest_potential":     "средний",
        "risk_level":           "средний",
        "liquidity_level":      "средняя",
        "liquidity_days":       90,
        "exit_strategy":        "",
        "strategy":             f"Дисконт {disc_pct}% к рынку. {'Нет заявок — можно взять по минимуму.' if parts_n==0 else f'{parts_n} участников.'} Проверьте документы перед подачей заявки.",
        "what_to_check":        "выписка ЕГРН актуальная, долги ЖКХ, прописанные лица, состояние объекта",
        "action":               action,
        "verdict":              action,
    }


async def analyze_lot(lot: dict) -> dict:
    title     = lot.get("title_full") or lot.get("title","")
    region    = lot.get("region","moskva")
    pdf_text  = lot.get("pdf_text","")
    page_text = lot.get("description","")
    analytics = lot.get("analytics_text","")
    lot_price = lot.get("price",0)
    lot_type  = lot.get("category","прочее")
    step_cur  = lot.get("step_current",0)
    step_tot  = lot.get("step_total",0)
    parts_n   = lot.get("participants",0)
    vin       = lot.get("vin","")
    cadastral = lot.get("cadastral","")
    rname     = "Москва" if "moskva" in region else "Московская область"
    if lot_price == 0:
        src = pdf_text or page_text
        for pat in [r'(\d[\d\s]{4,})\s*(?:руб|₽)',r'цена[^\d]*(\d[\d\s]{4,})']:
            m = re.search(pat, src, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s','',m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        lot_price = p; break
                except: pass
    mkt      = calc_market_price(lot_type, title, region)
    mkt_prc  = mkt["market_price"]
    rental   = mkt["rental_monthly"]
    disc_pct = 0
    if mkt_prc > 0 and lot_price > 0 and mkt_prc > lot_price:
        disc_pct = round((mkt_prc - lot_price) / mkt_prc * 100)
    score     = calc_score(lot_price, mkt_prc, rental, parts_n,
                           cadastral, step_cur, step_tot)
    step_info = f"Шаг {step_cur}/{step_tot} (осталось {step_tot-step_cur})" if step_cur else ""
    full_text = pdf_text or page_text[:1000]
    expert = await get_expert_analysis(
        title, lot_type, rname, lot_price, mkt_prc,
        disc_pct, rental, parts_n, step_info,
        cadastral, vin, full_text, analytics, score
    )
    roi_parts = []
    if rental > 0 and lot_price > 0:
        yld   = round(rental * 12 / lot_price * 100, 1)
        years = round(lot_price / (rental * 12), 1)
        roi_parts.append(f"Аренда {rental:,}₽/мес | {yld}% год | {years} лет")
    if disc_pct > 0 and mkt_prc > 0 and lot_price > 0:
        profit = mkt_prc - lot_price
        roi_parts.append(f"Перепродажа +{disc_pct}% (+{profit/1e6:.1f}млн₽)")
    roi_text = " | ".join(roi_parts) if roi_parts else "нет данных"
    def fmt(p):
        try:
            p = float(p)
            if p >= 1_000_000: return f"{p/1_000_000:.1f} млн ₽"
            elif p > 0:        return f"{int(p):,} ₽".replace(',',' ')
        except: pass
        return "уточните на сайте"
    action_map = {
        "ВХОДИТЬ СЕЙЧАС":      "🟢",
        "ЖДАТЬ СНИЖЕНИЯ":      "⏳",
        "ПРОВЕРИТЬ ДОКУМЕНТЫ": "⚠️",
        "ПРОПУСТИТЬ":          "🔴",
    }
    invest_icons = {"высокий":"🔥","средний":"📈","низкий":"📉"}
    risk_icons   = {"низкий":"🟢","средний":"🟡","высокий":"🟠","критический":"🔴"}
    ip     = expert.get("invest_potential","средний") or "средний"
    rl     = expert.get("risk_level","средний") or "средний"
    action = expert.get("action","ВХОДИТЬ СЕЙЧАС" if score>=8 else
                        "ЖДАТЬ СНИЖЕНИЯ" if score>=7 else "ПРОВЕРИТЬ ДОКУМЕНТЫ")
    risks = expert.get("invest_risks",[]) or []
    opps  = expert.get("invest_opportunities",[]) or []
    extra = []
    if cadastral: extra.append(f"🏛 Кадастр: {cadastral}")
    if vin:       extra.append(f"🔍 VIN: {vin}")
    if parts_n == 0:   extra.append("👥 Нет заявок — можно взять по минимуму")
    elif parts_n <= 2: extra.append(f"👥 {parts_n} участника — конкуренция низкая")
    elif parts_n > 5:  extra.append(f"👥 ⚠️ {parts_n} участников — высокая конкуренция!")
    else:              extra.append(f"👥 {parts_n} участника")
    if risks and risks[0] not in ("нет данных","нет данных из документов"):
        extra.append("⚠️ Риски: " + " | ".join(str(r) for r in risks[:2]))
    if opps and opps[0] not in ("требует анализа",):
        extra.append("✨ Плюсы: " + " | ".join(str(o) for o in opps[:2]))
    encumb = expert.get("encumbrances","") or ""
    exit_s = expert.get("exit_strategy","") or ""
    return {
        "lot_type":         lot_type,
        "total_score":      score,
        "score_label":      f"{'🔥' if score>=9 else '⭐' if score>=8 else '📊'} {score}/10",
        "price":            fmt(lot_price),
        "market_price":     fmt(mkt_prc),
        "market_comment":   mkt.get("comment",""),
        "discount_pct":     str(disc_pct) if disc_pct > 0 else "0",
        "step":             step_info,
        "liquidity_text":   f"{expert.get('liquidity_level','средняя')} (~{expert.get('liquidity_days',90)} дней)",
        "roi_text":         roi_text,
        "legal_text":       expert.get("legal_summary","требует проверки"),
        "encumbrances":     encumb if encumb not in ("уточните на сайте","") else "",
        "owners":           expert.get("owners_count","?"),
        "exit_strategy":    exit_s if exit_s not in ("уточните после проверки документов","") else "",
        "extra_checks":     "\n".join(extra),
        "risk_text":        f"{risk_icons.get(rl,'🟡')} риск: {rl}",
        "invest_text":      f"{invest_icons.get(ip,'📈')} потенциал: {ip}",
        "strategy":         expert.get("strategy",""),
        "what_to_check":    expert.get("what_to_check",""),
        "action":           action,
        "action_emoji":     action_map.get(action,"⚠️"),
        "verdict":          expert.get("verdict",action),
        "worth_showing":    True,
    }
