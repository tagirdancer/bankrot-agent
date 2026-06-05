"""
Анализатор v13.0 — с PDF и Росреестром
- Скачивает PDF через cookies авторизации
- Проверяет кадастр через Росреестр
- Полный юридический анализ
- Умный балл по формуле
"""
import httpx, json, re, os, asyncio, io, logging, time
from datetime import datetime
from urllib.parse import quote
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("analyzer")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_KEY = os.getenv("GROQ_API_KEY")
COOKIES  = os.getenv("TBANKROT_COOKIES", "")
MODEL    = "llama-3.1-8b-instant"
MIN_SCORE = 0.0


def format_short_lot_message(lot: dict, an: dict, label: str = "ЛОТ") -> str:
    """Короткий блок лота — единый формат для дайджеста и анализа по ссылке."""
    score = an.get("total_score", "?")
    disc = an.get("discount_pct", "?")
    verdict = an.get("verdict_simple") or an.get("action", "?")
    rn = f" 🌍 {lot.get('region', '')}" if lot.get("is_extra") else ""
    disc_s = f" (-{disc}%)" if str(disc) not in ("?", "0", "") else ""
    return (
        f"🔔 *{label} — {score}/10*{rn}\n"
        f"{lot.get('title', '')[:70]}\n"
        f"💰 {an.get('price', '—')} → рынок {an.get('market_price', '—')}{disc_s}\n"
        f"{an.get('action_emoji', '⚠️')} *{verdict}*\n"
        f"🔗 {lot.get('url', '')}"
    )


def deep_callback_data(lot_id: str, an: dict, lot: dict, parsed_at=None) -> str:
    """callback_data кнопки «Полный анализ» с реальными цифрами + timestamp спарсинга."""
    if parsed_at is None:
        ts = int(time.time())
    elif isinstance(parsed_at, datetime):
        ts = int(parsed_at.timestamp())
    else:
        ts = int(parsed_at)
    return (
        f"deep_{lot_id}_{int(an.get('lot_price_raw', 0) or 0)}_"
        f"{int(an.get('market_price_raw', 0) or 0)}_{an.get('discount_pct', '0')}_"
        f"{lot.get('participants', 0)}_{ts}"
    )


def build_verification_links(cadastral: str, address: str = "") -> str:
    """Прямые ссылки на проверку — без выдуманных данных."""
    parts = []
    kad = (cadastral or "").strip()
    addr = (address or "").strip()
    if kad:
        q = quote(kad)
        parts.append(f"кадастровая карта: https://nspd.gov.ru/map?thematic=PKK&query={q}")
        parts.append(f"Росреестр (ПКК): https://pkk.rosreestr.ru/#/search?text={q}")
    else:
        parts.append("кадастровый номер не найден в карточке")
    fssp = "https://fssp.gov.ru/iss/ip"
    if addr:
        parts.append(f"ФССП (банк исп. производств): {fssp} — поиск по адресу: {addr[:80]}")
    else:
        parts.append(f"ФССП (банк исп. производств): {fssp} — адрес в карточке не найден, введите вручную")
    return " | ".join(parts)


def enrich_what_to_check(wtc: str, cadastral: str, address: str = "") -> str:
    links = build_verification_links(cadastral, address)
    extra = f"проверка арестов/обременений: {links}"
    return f"{wtc}; {extra}" if wtc else extra


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
    if any(w in t for w in ["апартамент"]): return "апартаменты"
    if any(w in t for w in ["квартир","комнат","студи",
                             "однокомнат","двухкомнат","жилое помещение"]): return "квартира"
    if any(w in t for w in ["жилой дом","дача","коттедж","таунхаус",
                             "садовый дом","домовлад"]): return "дом"
    if any(w in t for w in ["нежилое","офис","торгов","магазин","склад",
                             "помещени","псн","ангар","цех"]): return "коммерция"
    if any(w in t for w in ["оборудован","станок","доля в ооо","дебиторск"]): return "бизнес"
    return "прочее"


async def download_pdf(lot_id: str) -> str:
    try:
        import pdfplumber
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer":    "https://tbankrot.ru/",
        }
        if COOKIES:
            headers["Cookie"] = COOKIES
        pdf_urls = [
            f"https://files.tbankrot.ru/egrn_files/{lot_id}.pdf",
            f"https://tbankrot.ru/files/egrn/{lot_id}.pdf",
            f"https://tbankrot.ru/item/egrn?id={lot_id}",
        ]
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for url in pdf_urls:
                try:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200 and b'%PDF' in resp.content[:10]:
                        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                            text = "\n".join(
                                p.extract_text() or "" for p in pdf.pages[:5]
                            )[:5000]
                            if text and len(text) > 100:
                                print(f"    📄 PDF скачан ({len(text)} симв.)")
                                return text
                except: continue
    except Exception as e:
        print(f"    PDF error: {e}")
    return ""


async def get_rosreestr_data(cadastral: str) -> str:
    if not cadastral:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://pkk.rosreestr.ru/api/features/5",
                params={"text": cadastral, "limit": 1, "skip": 0},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pkk.rosreestr.ru/"}
            )
            if resp.status_code == 200:
                data  = resp.json()
                feats = data.get("features", [])
                if feats:
                    attrs = feats[0].get("attrs", {})
                    parts = []
                    if attrs.get("address"):     parts.append(f"Адрес: {attrs['address']}")
                    if attrs.get("area_value"):  parts.append(f"Площадь: {attrs['area_value']}м²")
                    if attrs.get("cad_cost"):
                        cost = float(attrs["cad_cost"])
                        parts.append(f"Кад.стоимость: {cost/1e6:.1f}млн₽")
                    if attrs.get("category_type"): parts.append(f"Категория: {attrs['category_type']}")
                    if attrs.get("util_by_doc"):   parts.append(f"Назначение: {attrs['util_by_doc']}")
                    if parts:
                        result = " | ".join(parts)
                        print(f"    🏛 Росреестр: {result}")
                        return result
    except Exception as e:
        print(f"    Росреестр error: {e}")
    return ""


async def get_lot_details(url: str, page) -> dict:
    details = {
        "price": 0, "title_full": "", "description": "",
        "step_current": 0, "step_total": 0, "participants": 0,
        "vin": "", "cadastral": "", "address": "", "analytics_text": "",
        "pdf_text": "", "rosreestr_data": ""
    }
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
                    p = float(re.sub(r'\s', '', m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        details["price"] = p; break
                except: pass
        for pat in [r'шаг[^\d]*(\d+)[^\d]+(\d+)', r'(\d+)\s*/\s*(\d+)']:
            sm = re.search(pat, text, re.IGNORECASE)
            if sm:
                try:
                    c, t = int(sm.group(1)), int(sm.group(2))
                    if 0 < c <= t <= 20:
                        details["step_current"] = c; details["step_total"] = t; break
                except: pass
        for pat in [r'заявок[^\d]*(\d+)', r'(\d+)\s*заявк', r'участник[^\d]*(\d+)']:
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
        lot_id = re.search(r'id=(\d+)', url)
        if lot_id:
            lid = lot_id.group(1)
            pdf_text = await download_pdf(lid)
            if pdf_text: details["pdf_text"] = pdf_text
            try:
                await page.goto(f"https://tbankrot.ru/analytics/{lid}", timeout=15000)
                await page.wait_for_timeout(1500)
                analytics = await page.inner_text("body")
                if len(analytics) > 200:
                    details["analytics_text"] = analytics[:2000]
                    print(f"    📊 Аналитика скачана")
            except: pass
        if details["cadastral"]:
            details["rosreestr_data"] = await get_rosreestr_data(details["cadastral"])
        addr = ""
        if details.get("rosreestr_data"):
            am = re.search(r"Адрес:\s*([^|]+)", details["rosreestr_data"])
            if am:
                addr = am.group(1).strip()
        if not addr:
            for pat in [r"адрес[:\s]+([^\n]{10,120})", r"местонахождение[:\s]+([^\n]{10,120})"]:
                am = re.search(pat, text, re.IGNORECASE)
                if am:
                    addr = am.group(1).strip()[:120]
                    break
        details["address"] = addr
    except Exception as e:
        print(f"    details error: {e}")
    return details


def calc_market_price(lot_type: str, title: str, region: str) -> dict:
    area_m = re.search(r'(\d+[.,]?\d*)\s*(?:м²|кв\.?\s*м)', title, re.IGNORECASE)
    area   = float(area_m.group(1).replace(',', '.')) if area_m else 0
    reg    = "moskva" if "moskva" in region else "mo"
    rname  = "Москва" if reg == "moskva" else "Московская область"
    prices = {
        "квартира":  {"moskva": 260000, "mo": 130000},
        "дом":       {"moskva": 200000, "mo": 120000},
        "коммерция": {"moskva": 320000, "mo": 130000},
        "земля":     {"moskva": 80000,  "mo": 25000},
        "авто":      {"moskva": 1,      "mo": 1},
        "гараж":     {"moskva": 160000, "mo": 90000},
    }
    rentals = {
        "квартира":  {"moskva": 1600, "mo": 750},
        "коммерция": {"moskva": 3200, "mo": 1300},
        "дом":       {"moskva": 1100, "mo": 550},
        "гараж":     {"moskva": 550,  "mo": 280},
    }
    ppm    = prices.get(lot_type, {}).get(reg, 100000)
    rpm    = rentals.get(lot_type, {}).get(reg, 0)
    market = int(area * ppm) if area > 0 else 0
    # Защита от абсурдных оценок
    if lot_type == "земля" and market > 30_000_000:
        market = 0  # земля редко стоит больше 30 млн — вероятно ошибка площади
    if market > 0 and area > 10000:  # площадь больше 10000 м² подозрительна
        market = 0
    rental = int(area * rpm) if area > 0 and rpm > 0 else 0
    return {
        "market_price": market, "rental_monthly": rental,
        "price_per_sqm": ppm, "area": area,
        "comment": f"{area:.0f}м² × {ppm:,}₽/м² в {rname}" if area > 0 else f"типичная цена в {rname}"
    }


def calc_score(lot_price, market_price, rental, parts_n,
               cadastral, step_cur, step_tot, has_pdf=False) -> float:
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
    if has_pdf:   score += 0.2
    if step_cur and step_tot:
        left = step_tot - step_cur
        if left <= 1:   score += 0.7
        elif left <= 3: score += 0.4
    return round(min(10.0, max(1.0, score)), 1)


async def get_expert_analysis(title, lot_type, region_name, lot_price,
                               market_price, disc_pct, rental, parts_n,
                               step_info, cadastral, vin, pdf_text,
                               analytics_text, rosreestr_data, score) -> dict:
    all_docs = ""
    if pdf_text:       all_docs += f"\n=== ЕГРН/Документы ===\n{pdf_text[:1500]}"
    if analytics_text: all_docs += f"\n=== Аналитика торгов ===\n{analytics_text[:500]}"
    if rosreestr_data: all_docs += f"\n=== Данные Росреестра ===\n{rosreestr_data}"
    has_docs = len(all_docs) > 50

    prompt = f"""Ты опытный юрист и финансовый эксперт по банкротным торгам России.
Анализируй строго как {lot_type.upper()}.
═══ ОБЪЕКТ ═══
Тип: {lot_type.upper()}
Название: {title[:200]}
Регион: {region_name}
Цена торгов: {f'{lot_price:,.0f}₽' if lot_price else 'не определена'}
Рыночная цена: {f'{market_price:,.0f}₽' if market_price else 'не определена'}
Дисконт к рынку: {disc_pct}%
Участников в торгах: {parts_n}
{'⚠️ ВНИМАНИЕ: дисконт более 40% — проверь причину!' if disc_pct >= 40 else ''}
Аренда/мес: {f'{rental:,.0f}₽' if rental else 'нет'}
{step_info}
{f'Кадастровый номер: {cadastral}' if cadastral else 'Кадастр: не найден'}
{f'VIN: {vin}' if vin else ''}
Наличие документов: {'ДА — анализируй детально' if has_docs else 'НЕТ — анализируй по названию'}
═══ ДОКУМЕНТЫ ═══
{all_docs if all_docs else 'Документы не получены. Анализируй по типу объекта и региону.'}
Ответь ТОЛЬКО JSON:
{{
  "legal_summary": "X собственников. Обременения: ... Аресты: ...",
  "encumbrances": "ипотека Сбербанк (снимается при покупке) / нет обременений",
  "owners_count": "1",
  "has_hidden_risks": false,
  "legal_risks": ["риск 1"],
  "invest_risks": ["инвест риск 1"],
  "invest_opportunities": ["плюс 1 с цифрами", "плюс 2"],
  "invest_potential": "высокий",
  "risk_level": "низкий",
  "liquidity_level": "высокая",
  "liquidity_days": 45,
  "exit_strategy": "перепродажа за 30-60 дней / сдача в аренду X₽/мес",
  "strategy": "Конкретно: почему входить или нет. Ключевые цифры.",
  "what_to_check": "КОНКРЕТНО для {lot_type}: укажи 3-5 пунктов специфичных для этого объекта и его ситуации",
  "action": "ВХОДИТЬ СЕЙЧАС",
  "verdict": "РЕКОМЕНДУЕТСЯ К ПОКУПКЕ",
  "verdict_simple": "ОБЯЗАТЕЛЬНО напиши: БРАТЬ или НЕ БРАТЬ и одну причину. Например: БРАТЬ — дисконт 61% и нет заявок. Или: НЕ БРАТЬ — цена выше рынка."
}}
action: ВХОДИТЬ СЕЙЧАС / ЖДАТЬ СНИЖЕНИЯ / ПРОВЕРИТЬ ДОКУМЕНТЫ / ПРОПУСТИТЬ
invest_potential: высокий / средний / низкий
risk_level: низкий / средний / высокий / критический
Если дисконт > 50% и нет документов — risk_level = высокий"""

    if not GROQ_KEY:
        log.error("GROQ_API_KEY отсутствует в окружении — Groq-анализ невозможен, "
                  "использую запасной разбор по типу объекта")
    else:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
                    json={"model": MODEL,
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": 600, "temperature": 0.7}
                )
                if resp.status_code != 200:
                    log.error("Groq вернул статус %s: %s", resp.status_code, resp.text[:300])
                data = resp.json()
                if "choices" in data:
                    raw = data["choices"][0]["message"]["content"]
                    m = re.search(r'\{[\s\S]*\}', raw)
                    if m:
                        return json.loads(m.group())
                    log.error("В ответе Groq не найден JSON: %s", raw[:300])
                else:
                    log.error("Неожиданный ответ Groq: %s", str(data)[:300])
        except Exception:
            log.exception("full_analysis failed: ошибка запроса к Groq")

    action = ("ВХОДИТЬ СЕЙЧАС" if score >= 8 else
              "ЖДАТЬ СНИЖЕНИЯ" if score >= 7 else "ПРОВЕРИТЬ ДОКУМЕНТЫ")
    rl   = "высокий" if disc_pct >= 50 and not pdf_text else "средний"
    opps = []
    if disc_pct > 0: opps.append(f"дисконт {disc_pct}% к рынку")
    if parts_n == 0: opps.append("нет конкурентов — цена минимальная")
    if rental > 0 and lot_price > 0:
        yld = round(rental * 12 / lot_price * 100, 1)
        opps.append(f"доходность аренды {yld}% годовых")

    checks = []
    if lot_type == "квартира":
        checks += ["выписка ЕГРН (собственники, аресты)",
                   "долги по ЖКХ и взносам на капремонт",
                   "кто прописан, есть ли несовершеннолетние"]
    elif lot_type == "дом":
        checks += ["право на дом и на землю под ним",
                   "категория земли и разрешённое использование",
                   "подключение коммуникаций (газ, вода, свет)"]
    elif lot_type == "коммерция":
        checks += ["назначение помещения и возможность аренды",
                   "действующие договоры аренды и арендаторы",
                   "отдельный вход, мощности, парковка"]
    elif lot_type == "земля":
        checks += ["категория земли и вид разрешённого использования",
                   "межевание и точные границы участка",
                   "обременения, сервитуты, охранные зоны"]
    elif lot_type == "авто":
        checks += ["проверка по VIN на гибдд.рф (аресты, ДТП)",
                   "залоги в реестре уведомлений ФНП",
                   "фактическое состояние и комплектность"]
    elif lot_type == "гараж":
        checks += ["оформлено ли право собственности (а не пай)",
                   "право на землю под гаражом",
                   "задолженность кооперативу"]
    else:
        checks += ["правоустанавливающие документы / выписка ЕГРН",
                   "обременения и аресты",
                   "фактическое состояние объекта"]
    if disc_pct >= 50:
        checks.append(f"проверьте причину большого дисконта {disc_pct}%")
    if parts_n == 0:
        checks.append("почему нет заявок при такой цене")
    elif parts_n > 5:
        checks.append(f"высокая конкуренция: уже {parts_n} участников")
    if not has_docs:
        checks.append("запросить документы у организатора — сейчас их нет")
    wtc = "; ".join(f"{i + 1}. {c}" for i, c in enumerate(checks[:5]))
    wtc = enrich_what_to_check(wtc, cadastral, "")

    return {
        "legal_summary":    "документы не получены — требует ручной проверки",
        "encumbrances":     "",
        "owners_count":     "?",
        "has_hidden_risks": disc_pct >= 50,
        "legal_risks":      ["документы не получены"],
        "invest_risks":     [f"дисконт {disc_pct}% требует объяснения" if disc_pct >= 40 else "стандартные риски"],
        "invest_opportunities": opps or ["требует анализа"],
        "invest_potential": "высокий" if disc_pct >= 40 else "средний",
        "risk_level":       rl,
        "liquidity_level":  "средняя",
        "liquidity_days":   90,
        "exit_strategy":    "",
        "strategy":         f"Дисконт {disc_pct}% к рынку. {'Нет заявок — можно взять по минимуму. ' if parts_n==0 else f'{parts_n} участников. '}{'⚠️ Большой дисконт — проверьте причину.' if disc_pct >= 50 else 'Проверьте документы.'}",
        "what_to_check":    wtc,
        "action":           action,
        "verdict":          action,
    }


async def analyze_lot(lot: dict) -> dict:
    title     = lot.get("title_full") or lot.get("title", "")
    region    = lot.get("region", "moskva")
    pdf_text  = lot.get("pdf_text", "")
    page_text = lot.get("description", "")
    analytics = lot.get("analytics_text", "")
    rosreestr = lot.get("rosreestr_data", "")
    lot_price = lot.get("price", 0)
    lot_type  = lot.get("category", "прочее")
    step_cur  = lot.get("step_current", 0)
    step_tot  = lot.get("step_total", 0)
    parts_n   = lot.get("participants", 0)
    vin       = lot.get("vin", "")
    cadastral = lot.get("cadastral", "")
    address   = lot.get("address", "")
    rname     = "Москва" if "moskva" in region else "Московская область"

    if lot_price == 0:
        src = pdf_text or page_text
        for pat in [r'(\d[\d\s]{4,})\s*(?:руб|₽)', r'цена[^\d]*(\d[\d\s]{4,})']:
            m = re.search(pat, src, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s', '', m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        lot_price = p; break
                except: pass

    mkt      = calc_market_price(lot_type, title, region)
    mkt_prc  = mkt["market_price"]
    rental   = mkt["rental_monthly"]
    disc_pct = 0
    if mkt_prc > 0 and lot_price > 0 and mkt_prc > lot_price:
        disc_pct = round((mkt_prc - lot_price) / mkt_prc * 100)
        # Дисконт выше 85% почти всегда ошибка оценки — не доверяем
        if disc_pct >= 85:
            disc_pct = 0
            mkt_prc = 0  # сбрасываем недостоверную рыночную цену
    has_pdf   = len(pdf_text) > 100
    if has_pdf:
        document_status = "Документы проверены"
    elif lot.get("pdf_found"):
        document_status = "PDF есть, не удалось прочитать"
    else:
        document_status = "Документы не получены"
    score     = calc_score(lot_price, mkt_prc, rental, parts_n,
                           cadastral, step_cur, step_tot, has_pdf)
    step_info = f"Шаг {step_cur}/{step_tot} (осталось {step_tot-step_cur})" if step_cur else ""
    urgency = ""
    if step_cur and step_tot:
        left = step_tot - step_cur
        if left == 0:
            urgency = "🚨 ПОСЛЕДНИЙ ШАГ — торги закрываются!"
        elif left == 1:
            urgency = "⏰ Предпоследний шаг — цена не снизится"
        elif left <= 3:
            urgency = f"⏳ Осталось {left} снижения цены"
    full_text = pdf_text or page_text[:1000]
    expert    = await get_expert_analysis(
        title, lot_type, rname, lot_price, mkt_prc,
        disc_pct, rental, parts_n, step_info,
        cadastral, vin, full_text, analytics, rosreestr, score
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
            elif p > 0:        return f"{int(p):,} ₽".replace(',', ' ')
        except: pass
        return "уточните на сайте"

    action_map   = {"ВХОДИТЬ СЕЙЧАС":"🟢","ЖДАТЬ СНИЖЕНИЯ":"⏳","ПРОВЕРИТЬ ДОКУМЕНТЫ":"⚠️","ПРОПУСТИТЬ":"🔴"}
    invest_icons = {"высокий":"🔥","средний":"📈","низкий":"📉"}
    risk_icons   = {"низкий":"🟢","средний":"🟡","высокий":"🟠","критический":"🔴"}
    ip = expert.get("invest_potential","средний") or "средний"
    rl_raw = expert.get("risk_level","средний") or "средний"
    if disc_pct >= 60 and not has_pdf:
        rl = "высокий"
    elif disc_pct >= 40 and not has_pdf and rl_raw == "низкий":
        rl = "средний"
    elif disc_pct >= 60 and has_pdf and rl_raw in ("средний","высокий"):
        rl = "средний"
    else:
        rl = rl_raw
    action = expert.get("action",
                        "ВХОДИТЬ СЕЙЧАС" if score>=8 else
                        "ЖДАТЬ СНИЖЕНИЯ" if score>=7 else "ПРОВЕРИТЬ ДОКУМЕНТЫ")
    risks = expert.get("invest_risks",[]) or []
    opps  = expert.get("invest_opportunities",[]) or []

    extra = []
    if disc_pct >= 60:
        extra.append(f"⚠️ Дисконт {disc_pct}% — проверьте причину низкой цены!")
    elif disc_pct >= 40:
        extra.append(f"💡 Дисконт {disc_pct}% — выгодно при чистых документах")
    if cadastral:
        cad_line = f"🏛 Кадастр: {cadastral}"
        if rosreestr:
            cad_line += f"\n   📍 {rosreestr}"
        extra.append(cad_line)
    if vin: extra.append(f"🔍 VIN: {vin} — проверьте на гибдд.рф")
    if has_pdf:
        pass  # статус PDF отражён в legal_text
    if parts_n == 0:
        extra.append("👥 Нет заявок — можно взять по стартовой цене")
    elif parts_n == 1:
        extra.append(f"👥 1 участник — конкуренция низкая")
    elif parts_n <= 3:
        extra.append(f"👥 {parts_n} участника — умеренная конкуренция")
    elif parts_n > 5:
        extra.append(f"👥 ⚠️ {parts_n} участников — высокая конкуренция!")
    valid_risks = [r for r in risks if r not in
                   ("нет данных","документы не получены — проверьте перед покупкой",
                    "требует проверки","стандартные риски")]
    valid_opps  = [o for o in opps if o not in ("требует анализа",)]
    if valid_risks:
        extra.append("⚠️ Риски: " + " | ".join(str(r) for r in valid_risks[:2]))
    if valid_opps:
        extra.append("✨ Плюсы: " + " | ".join(str(o) for o in valid_opps[:2]))
    verdict_simple = expert.get("verdict_simple","")
    if verdict_simple:
        extra.append(f"🎯 {verdict_simple}")
    encumb = expert.get("encumbrances","") or ""
    exit_s = expert.get("exit_strategy","") or ""

    return {
        "lot_type":       lot_type,
        "total_score":    score,
        "score_label":    f"{'🔥' if score>=9 else '⭐' if score>=8 else '📊'} {score}/10",
        "price":          fmt(lot_price),
        "market_price":   fmt(mkt_prc),
        "market_comment": mkt.get("comment",""),
        "discount_pct":   str(disc_pct) if disc_pct > 0 else "0",
        "lot_price_raw":  lot_price,
        "market_price_raw": mkt_prc,
        "step":           step_info,
        "liquidity_text": f"{expert.get('liquidity_level','средняя')} (~{expert.get('liquidity_days',90)} дней)",
        "roi_text":       roi_text,
        "legal_text":     expert.get("legal_summary","требует проверки"),
        "document_status": document_status,
        "encumbrances":   encumb if encumb not in ("уточните на сайте","") else "",
        "owners":         expert.get("owners_count","?"),
        "exit_strategy":  exit_s if exit_s not in ("уточните после проверки документов","") else "",
        "extra_checks":   "\n".join(extra),
        "risk_text":      f"{risk_icons.get(rl,'🟡')} риск: {rl}",
        "invest_text":    f"{invest_icons.get(ip,'📈')} потенциал: {ip}",
        "strategy":       expert.get("strategy",""),
        "what_to_check":  enrich_what_to_check(expert.get("what_to_check", ""), cadastral, address),
        "action":         action,
        "action_emoji":   action_map.get(action,"⚠️"),
        "verdict":        expert.get("verdict",action),
        "has_pdf":        has_pdf,
        "urgency":        urgency,
        "verdict_simple": expert.get("verdict_simple",""),
        "worth_showing":  True,
    }
