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

REGION_LABELS = {
    "moskva": "Москва", "moskovskaya-oblast": "Московская область",
    "sankt-peterburg": "Санкт-Петербург", "leningradskaya-oblast": "Ленинградская область",
    "krasnodar": "Краснодар", "ekaterinburg": "Екатеринбург", "novosibirsk": "Новосибирск",
    "tatarstan": "Татарстан", "bashkortostan": "Башкортостан", "rostov-na-donu": "Ростов-на-Дону",
    "samara": "Самара", "nizhegorodskaya-oblast": "Нижний Новгород",
    "volgogradskaya-oblast": "Волгоград", "krasnoyarskiy-kray": "Красноярск",
}

# Красные флаги — только если слово реально есть в тексте карточки
RED_FLAGS_HOUSING = {
    "арест": "арест", "залог": "залог", "обременен": "обременение",
    "ипотек": "ипотека", "прописан": "прописанные", "зарегистрирован": "зарегистрированные",
    "несовершеннолетн": "несовершеннолетние", "оспариван": "оспаривание",
    "доля": "доля в праве", "не осмотрен": "не осмотрен", "нет доступа": "нет доступа",
    "самовольн": "самовольная постройка", "реконструкц": "реконструкция",
}
RED_FLAGS_LAND = {
    "сельхоз": "сельхозназначение", "сельскохоз": "сельхозназначение",
    "коммуникац": "нет/неизвестны коммуникации", "подъезд": "проблемы с подъездом",
    "дорог": "проблемы с дорогой", "водоохран": "водоохранная зона",
    "санитарн": "санитарная зона", "лесной фонд": "лесной фонд", "лесфонд": "лесной фонд",
    "не размежев": "не размежёван", "размежеван": "спор о границах",
    "границ": "спор о границах",
}


def parse_card_meta(text: str) -> dict:
    """Поля только из текста карточки tbankrot (regex, без выдумок)."""
    t = text or ""
    tl = t.lower()
    meta = {
        "auction_format": "", "application_deadline": "", "deposit": 0,
        "next_reduction_date": "", "next_reduction_price": 0,
        "area_sqm": 0, "area_sotka": 0,
    }
    if "публичн" in tl and "предложен" in tl:
        meta["auction_format"] = "публичное предложение"
    elif "аукцион" in tl:
        meta["auction_format"] = "аукцион"
    for pat in [
        r"(?:при[её]м|подач)[а-я\s]{0,20}заявок[^\d]{0,30}(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
        r"заявок[^\d]{0,20}до[^\d]{0,10}(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
        r"окончан[иея][^\d]{0,30}заявок[^\d]{0,20}(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})",
    ]:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            meta["application_deadline"] = m.group(1)
            break
    dm = re.search(r"задаток[^\d]{0,20}(\d[\d\s]{2,})\s*(?:руб|₽)", t, re.IGNORECASE)
    if dm:
        try:
            meta["deposit"] = int(re.sub(r"\s", "", dm.group(1)))
        except Exception:
            pass
    if meta["auction_format"] == "публичное предложение":
        for pat in [
            r"следующ[^\d]{0,40}(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})[^\d]{0,40}(\d[\d\s]{4,})\s*(?:руб|₽)",
            r"снижен[иея][^\d]{0,40}(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})[^\d]{0,40}(\d[\d\s]{4,})\s*(?:руб|₽)",
        ]:
            m = re.search(pat, t, re.IGNORECASE)
            if m:
                meta["next_reduction_date"] = m.group(1)
                try:
                    meta["next_reduction_price"] = int(re.sub(r"\s", "", m.group(2)))
                except Exception:
                    pass
                break
    sm = re.search(r"(\d+[.,]?\d*)\s*(?:м²|кв\.?\s*м)", t, re.IGNORECASE)
    if sm:
        try:
            meta["area_sqm"] = float(sm.group(1).replace(",", "."))
        except Exception:
            pass
    st = re.search(r"(\d+[.,]?\d*)\s*(?:сот|соток|сот\.?)", t, re.IGNORECASE)
    if st:
        try:
            meta["area_sotka"] = float(st.group(1).replace(",", "."))
        except Exception:
            pass
    return meta


def parse_land_meta(text: str) -> dict:
    """Отдельная логика для земли — только из текста карточки."""
    tl = (text or "").lower()
    vri = ""
    for key, label in [
        ("ижс", "ИЖС"), ("индивидуальн", "ИЖС"), ("снт", "СНТ"), ("садовод", "СНТ"),
        ("сельхоз", "сельхоз"), ("сельскохоз", "сельхоз"), ("промышлен", "промышленная"),
        ("коммерч", "коммерческая"), ("лпх", "ЛПХ"),
    ]:
        if key in tl:
            vri = label
            break
    flags = scan_red_flags(text, "земля")
    return {"land_vri": vri, "land_flags": flags}


def scan_red_flags(text: str, lot_type: str) -> list:
    tl = (text or "").lower()
    found = []
    patterns = RED_FLAGS_LAND if lot_type == "земля" else RED_FLAGS_HOUSING
    seen = set()
    for key, label in patterns.items():
        if key in tl and label not in seen:
            found.append(label)
            seen.add(label)
    return found


def build_trading_summary(lot: dict) -> str:
    """Строка сроков/стадии — только если поля реально спарсились."""
    parts = []
    fmt = lot.get("auction_format", "")
    if fmt:
        parts.append(fmt)
    dl = lot.get("application_deadline", "")
    if dl:
        parts.append(f"заявки до {dl}")
    dep = lot.get("deposit", 0)
    if dep:
        parts.append(f"задаток {dep:,} ₽".replace(",", " "))
    sc, st = lot.get("step_current", 0), lot.get("step_total", 0)
    if sc and st:
        parts.append(f"шаг {sc}/{st}")
    if fmt == "публичное предложение":
        nd, np = lot.get("next_reduction_date", ""), lot.get("next_reduction_price", 0)
        if nd and np:
            p = f"{np/1e6:.1f} млн ₽" if np >= 1_000_000 else f"{np:,} ₽".replace(",", " ")
            parts.append(f"след. снижение {nd} → {p}")
    return " | ".join(parts)


def format_short_lot_message(lot: dict, an: dict, label: str = "ЛОТ") -> str:
    """Короткий блок лота — единый формат для дайджеста и анализа по ссылке."""
    score = an.get("total_score", "?")
    verdict = an.get("verdict_label") or an.get("verdict_simple") or an.get("action", "?")
    rn = f" 🌍 {lot.get('region', '')}" if lot.get("is_extra") else ""
    lines = [
        f"🔔 *{label} — {score}/10*{rn}",
        f"{lot.get('title', '')[:70]}",
    ]
    if an.get("dedup_note"):
        lines.append(f"_{an['dedup_note']}_")
    lines.append(an.get("price_line") or format_price_line(an))
    if an.get("document_status"):
        lines.append(f"📄 {an['document_status']}")
    if an.get("lot_type") == "авто" and an.get("auto_summary"):
        lines.append(f"🚗 {an['auto_summary']}")
    elif an.get("legal_text"):
        lines.append(f"📋 {an['legal_text'][:140]}")
    trading = an.get("trading_summary", "")
    if trading:
        lines.append(f"📋 {trading}")
    if an.get("risk_level"):
        lines.append(
            f"⚖️ Риск: {an['risk_level']} ({an.get('risk_score', '?')}/100) | "
            f"Инвест: {an.get('invest_score', '?')}/100"
        )
    flags = an.get("red_flags_text", "")
    if flags:
        lines.append(f"⚠️ Флаги: {flags}")
    if lot_type := an.get("lot_type"):
        if lot_type == "земля" and lot.get("land_vri"):
            lines.append(f"🌱 Земля: {lot.get('land_vri')}")
    detail = an.get("verdict_detail", "")
    detail_s = f" — {detail[:70]}" if detail else ""
    lines += [
        f"{an.get('action_emoji', '⚠️')} *{verdict}*{detail_s}",
        f"🔗 {lot.get('url', '')}",
    ]
    return "\n".join(lines)


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


def lot_action_keyboard(lot_id: str, an: dict, lot: dict, parsed_at=None):
    """Кнопки под лотом: Полный анализ + Сохранить."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Полный анализ",
                             callback_data=deep_callback_data(lot_id, an, lot, parsed_at)),
        InlineKeyboardButton("⭐ Сохранить", callback_data=f"save_{lot_id}"),
    ]])


def is_real_estate(lot_type: str) -> bool:
    return lot_type in ("квартира", "апартаменты", "дом", "коммерция", "земля", "гараж")


def parse_egrn_pdf(text: str) -> dict:
    """Извлекает поля из текста выписки ЕГРН (PDF)."""
    result = {
        "cadastral": "", "address": "", "area": "",
        "owner": "", "encumbrances": "", "summary": "",
    }
    if not text or len(text) < 80:
        return result
    kad = re.search(r"\b(\d{2}:\d{2}:\d{6,7}:\d+)\b", text)
    if kad:
        result["cadastral"] = kad.group(1)
    for pat in [
        r"(?:местоположени[ея]|адрес(?:\(местоположение\))?)[:;\s]+([^\n]{15,200})",
        r"(?:находится по адресу)[:;\s]+([^\n]{15,200})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            addr = re.sub(r"\s{2,}", " ", m.group(1).strip())
            result["address"] = addr[:150]
            break
    am = re.search(
        r"площад[ьи][^\d]{0,30}(\d+[.,]?\d*)\s*(?:кв\.?\s*м|м²|кв\.м|кв\.?\s*м\.)",
        text, re.IGNORECASE,
    )
    if am:
        result["area"] = am.group(1).replace(",", ".") + " м²"
    for pat in [
        r"правообладател(?:ь|и)[:\s]+([^\n]{5,120})",
        r"собственник[:\s]+([^\n]{5,120})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if len(val) > 4 and not re.fullmatch(r"[\d\s]+", val):
                result["owner"] = val[:100]
                break
    if re.search(r"нет зарегистрированных обремен|обременен.*не зарегистрир|без обремен|обременения отсутств", text, re.I):
        result["encumbrances"] = "нет (по тексту ЕГРН)"
    else:
        enc = []
        for pat in [
            r"вид\s+обременени[яь][:\s]+([^\n]{5,120})",
            r"обременени[ея][:\s]+([^\n]{5,120})",
            r"ограничени[ея]\s+прав[^\n]{0,20}([^\n]{5,120})",
        ]:
            for m in re.finditer(pat, text, re.IGNORECASE):
                v = m.group(1).strip()
                if v and v not in enc:
                    enc.append(v[:80])
        if enc:
            result["encumbrances"] = "; ".join(enc[:3])[:200]
        elif re.search(r"ипотек|залог|арест", text, re.I):
            result["encumbrances"] = "упоминаются в тексте ЕГРН"
    parts = []
    if result["cadastral"]:
        parts.append(f"кадастр {result['cadastral']}")
    if result["address"]:
        parts.append(result["address"][:70])
    if result["area"]:
        parts.append(result["area"])
    if result["owner"]:
        parts.append(f"правообладатель: {result['owner'][:50]}")
    if result["encumbrances"]:
        parts.append(f"обременения: {result['encumbrances'][:50]}")
    result["summary"] = " | ".join(parts)
    return result


def apply_egrn_to_lot(lot: dict, pdf_text: str, from_real_pdf: bool) -> None:
    """Парсит ЕГРН и дополняет lot; from_real_pdf=False — не считаем «документы проверены»."""
    lot["pdf_from_egrn"] = from_real_pdf
    if from_real_pdf:
        lot["egrn_pdf_text"] = pdf_text
    egrn = parse_egrn_pdf(pdf_text) if from_real_pdf else {}
    lot["egrn_parsed"] = egrn
    if from_real_pdf:
        lot["pdf_text"] = pdf_text
    if egrn.get("cadastral") and not lot.get("cadastral"):
        lot["cadastral"] = egrn["cadastral"]
    if egrn.get("address") and not lot.get("address"):
        lot["address"] = egrn["address"]
    if egrn.get("area") and not lot.get("area_sqm"):
        try:
            lot["area_sqm"] = float(egrn["area"].replace(" м²", "").replace(",", "."))
        except ValueError:
            pass


def resolve_document_status(lot: dict) -> str:
    """Единый статус документов — одно значение на весь лот."""
    if lot.get("pdf_from_egrn") and lot.get("egrn_pdf_text"):
        return "Документы проверены (ЕГРН)"
    if lot.get("pdf_download_failed"):
        return "PDF есть, не прочитан"
    return "Документы не получены"


def parse_auto_meta(text: str) -> dict:
    """Мета для транспорта: VIN, год, пробег — только из текста."""
    meta = {"vin": "", "year": "", "mileage": "", "brand_hint": ""}
    vin = re.search(r"\b([A-HJ-NPR-Z0-9]{17})\b", (text or "").upper())
    if vin:
        meta["vin"] = vin.group(1)
    ym = re.search(r"(?:г\.?\s*в\.?|год выпуска|выпуск)[^\d]{0,15}(\d{4})", text or "", re.I)
    if ym:
        meta["year"] = ym.group(1)
    mm = re.search(r"(\d[\d\s]{1,7})\s*(?:км|kilomet)", text or "", re.I)
    if mm:
        meta["mileage"] = re.sub(r"\s", "", mm.group(1)) + " км"
    for brand in ["камаз", "уаз", "газ", "ваз", "lada", "bmw", "mercedes", "toyota"]:
        if brand in (text or "").lower():
            meta["brand_hint"] = brand.upper()
            break
    return meta


def build_auto_verification_links(vin: str = "") -> str:
    lines = []
    v = (vin or "").strip()
    if v:
        lines.append(f"[Реестр залогов ФНП](https://www.reestr-zalogov.ru/search/index) — VIN: `{v}`")
        lines.append(f"[ГИБДД — проверка ТС](https://гибдд.рф/check/auto) — VIN: `{v}`")
    else:
        lines.append("VIN не найден в карточке — проверьте описание лота")
    lines.append("[ФССП — банк данных](https://fssp.gov.ru/iss/ip)")
    return "\n".join(lines)


def build_verification_links(cadastral: str, address: str = "", vin: str = "", lot_type: str = "") -> str:
    """Прямые ссылки с подставленными данными."""
    if lot_type == "авто":
        return build_auto_verification_links(vin)
    lines = []
    kad = (cadastral or "").strip()
    addr = (address or "").strip()
    if kad:
        q = quote(kad)
        lines.append(f"[Кадастровая карта НСПД](https://nspd.gov.ru/map?thematic=PKK&query={q})")
        lines.append(f"[Росреестр ПКК](https://pkk.rosreestr.ru/#/search?text={q})")
    else:
        lines.append("кадастровый номер не найден — см. карточку лота или ЕГРН")
    if addr:
        lines.append(f"[ФССП — банк данных](https://fssp.gov.ru/iss/ip) — адрес для поиска: _{addr[:80]}_")
        aq = quote(addr[:120])
        lines.append(f"[Яндекс-карты (локация)](https://yandex.ru/maps/?text={aq})")
    else:
        lines.append("[ФССП — банк данных](https://fssp.gov.ru/iss/ip)")
    return "\n".join(lines)


def enrich_what_to_check(wtc: str, cadastral: str, address: str = "",
                         vin: str = "", lot_type: str = "") -> str:
    links = build_verification_links(cadastral, address, vin, lot_type)
    label = "проверки по авто" if lot_type == "авто" else "проверка арестов/обременений"
    extra = f"{label}:\n{links}"
    return f"{wtc}\n{extra}" if wtc else extra


def format_price_line(an: dict) -> str:
    """Строка цены — без ложного дисконта при неизвестном рынке."""
    price = an.get("price", "—")
    lot_type = an.get("lot_type", "")
    if not an.get("market_known"):
        if lot_type == "авто":
            return f"💰 {price} | _оценить рынок авто вручную_"
        if lot_type == "земля" or an.get("land_manual_market"):
            return f"💰 {price} | _оценить рынок земли вручную_"
        return f"💰 {price} | _рынок не определён — оценить вручную_"
    disc = an.get("discount_pct", "0")
    disc_s = f" (-{disc}%)" if str(disc) not in ("0", "?", "") else ""
    return f"💰 {price} → рынок {an.get('market_price', '—')}{disc_s}"


def detect_type(text: str) -> str:
    t = text.lower()
    auto = [
        "автомобил", "легков", "грузов", "седан", "хэтчбек", "внедорожник",
        "кроссовер", "автобус", "мотоцикл", "прицеп", "спецтехник",
        "экскаватор", "трактор", "погрузчик", "самосвал", "камаз", "газель",
        "уаз", "ваз", "lada", "bmw", "mercedes", "benz", "toyota", "hyundai",
        "kia", "volkswagen", "ford", "renault", "nissan", "mazda", "honda",
        "audi", "volvo", "skoda", "peugeot", "citroen", "mitsubishi",
        "subaru", "lexus", "porsche", "chery", "geely", "haval",
        "тойота", "хендай", "киа", "форд", "рено", "ниссан", "мазда",
        "движимое имущество", "военное", "транспортное средств", "автомашин",
    ]
    if any(w in t for w in auto):
        return "авто"
    if any(w in t for w in ["гараж", "машиноместо", "парковочн"]):
        return "гараж"
    if any(w in t for w in ["земельн", "участок", " га ", "гектар", "снт ", "ижс", "лпх "]):
        if not any(w in t for w in ["квартир", "комнат", "студи"]):
            return "земля"
    if any(w in t for w in ["апартамент"]):
        return "апартаменты"
    if any(w in t for w in ["квартир", "комнат", "студи",
                             "однокомнат", "двухкомнат", "жилое помещение"]):
        return "квартира"
    if any(w in t for w in ["жилой дом", "дача", "коттедж", "таунхаус",
                             "садовый дом", "домовлад"]):
        return "дом"
    if any(w in t for w in ["нежилое", "офис", "торгов", "магазин", "склад",
                             "помещени", "псн", "ангар", "цех"]):
        return "коммерция"
    if any(w in t for w in ["оборудован", "станок", "доля в ооо", "дебиторск"]):
        return "бизнес"
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
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False) as client:
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
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
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


async def get_lot_details(url: str, page, light: bool = False) -> dict:
    """light=True — только карточка лота, без analytics/Росреестра/PDF."""
    details = {
        "price": 0, "title_full": "", "description": "",
        "step_current": 0, "step_total": 0, "participants": 0,
        "vin": "", "cadastral": "", "address": "", "analytics_text": "",
        "pdf_text": "", "rosreestr_data": ""
    }
    goto_ms = 12000 if light else 22000
    wait_ms = 600 if light else 1500
    try:
        await page.goto(url, timeout=goto_ms)
        await page.wait_for_timeout(wait_ms)
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
        pre_cat = detect_type(f"{details.get('title_full', '')} {text[:800]}")
        if not light and lot_id and pre_cat != "авто":
            lid = lot_id.group(1)
            # PDF качается в agent.enrich_heavy после login (без cookies → 403)
            try:
                await page.goto(f"https://tbankrot.ru/analytics/{lid}", timeout=12000)
                await page.wait_for_timeout(1000)
                analytics = await page.inner_text("body")
                if len(analytics) > 200:
                    details["analytics_text"] = analytics[:2000]
                    print(f"    📊 Аналитика скачана")
            except Exception:
                pass
        if not light and pre_cat != "авто" and details.get("cadastral"):
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
        card = parse_card_meta(text)
        details.update(card)
    except Exception as e:
        print(f"    details error: {e}")
    return details


def calc_market_price(lot_type: str, title: str, region: str, description: str = "") -> dict:
    src = f"{title} {description[:500]}"
    area_m = re.search(r'(\d+[.,]?\d*)\s*(?:м²|кв\.?\s*м)', src, re.IGNORECASE)
    area_s = re.search(r'(\d+[.,]?\d*)\s*(?:сот|соток)', src, re.IGNORECASE)
    area   = float(area_m.group(1).replace(',', '.')) if area_m else 0
    sotka  = float(area_s.group(1).replace(',', '.')) if area_s else 0
    if region == "moskva":
        reg = "moskva"
    elif "moskovskaya" in region:
        reg = "mo"
    else:
        reg = "other"
    rname  = REGION_LABELS.get(region, region)

    # Земля — не считаем рынок по м² как для жилья
    if lot_type == "земля":
        return {
            "market_price": 0, "rental_monthly": 0, "price_per_sqm": 0,
            "area": area or sotka, "area_sotka": sotka,
            "comment": "оценить рынок земли вручную",
            "manual_market": True,
        }
    prices = {
        "квартира":  {"moskva": 260000, "mo": 130000, "other": 120000},
        "дом":       {"moskva": 200000, "mo": 120000, "other": 100000},
        "коммерция": {"moskva": 320000, "mo": 130000, "other": 110000},
        "земля":     {"moskva": 80000,  "mo": 25000,  "other": 20000},
        "авто":      {"moskva": 1,      "mo": 1,       "other": 1},
        "гараж":     {"moskva": 160000, "mo": 90000,  "other": 80000},
    }
    ppm    = prices.get(lot_type, {}).get(reg, 100000)
    market = int(area * ppm) if area > 0 else 0
    if market > 0 and area > 10000:
        market = 0
    return {
        "market_price": market, "rental_monthly": 0,
        "price_per_sqm": ppm, "area": area,
        "comment": f"{area:.0f}м² × {ppm:,}₽/м² в {rname}" if area > 0 else "",
        "manual_market": False,
    }


def calc_score(lot_price, market_price, parts_n, cadastral, step_cur, step_tot,
               has_pdf=False, red_flags=None, deadline_str="", disc_pct=0) -> float:
    """Балл: меньше веса дисконту, штраф за флаги, бонус за запас по сроку."""
    score = 6.0
    red_flags = red_flags or []
    if disc_pct >= 50:
        score -= 1.0
    elif disc_pct >= 40:
        score += 0.3
    elif disc_pct >= 30:
        score += 0.6
    elif disc_pct >= 20:
        score += 0.8
    elif disc_pct >= 10:
        score += 0.4
    score -= min(2.5, len(red_flags) * 0.5)
    if parts_n == 0:
        score += 0.3
    elif parts_n <= 2:
        score += 0.1
    elif parts_n > 5:
        score -= 0.4
    if cadastral:
        score += 0.2
    if has_pdf:
        score += 0.2
    if step_cur and step_tot:
        left = step_tot - step_cur
        if left >= 3:
            score += 0.3
        elif left <= 1:
            score -= 0.2
    if deadline_str:
        try:
            from datetime import datetime as dt
            for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    d = dt.strptime(deadline_str[:10], fmt)
                    days = (d - dt.now()).days
                    if days >= 7:
                        score += 0.3
                    elif days <= 2:
                        score -= 0.3
                    break
                except ValueError:
                    continue
        except Exception:
            pass
    return round(min(10.0, max(1.0, score)), 1)


def expert_fallback(title, lot_type, lot_price, market_price, disc_pct, parts_n,
                  score, cadastral, vin, pdf_text, has_docs=False) -> dict:
    """Запасной разбор без Groq — детерминированный."""
    action = ("ВХОДИТЬ СЕЙЧАС" if score >= 8 else
              "ЖДАТЬ СНИЖЕНИЯ" if score >= 7 else "ПРОВЕРИТЬ ДОКУМЕНТЫ")
    rl = "высокий" if disc_pct >= 50 and not pdf_text else "средний"
    opps = []
    market_known = market_price > 0
    if market_known and disc_pct > 0:
        opps.append(f"дисконт {disc_pct}% к рынку")
    if parts_n == 0 and market_known:
        opps.append("нет заявок — можно взять по стартовой цене")
    elif parts_n == 0:
        opps.append("нет заявок")
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
    if market_known and disc_pct >= 50:
        checks.append(f"проверьте причину большого дисконта {disc_pct}%")
    if parts_n == 0 and market_known:
        checks.append("почему нет заявок при такой цене")
    elif parts_n == 0:
        checks.append("почему нет заявок")
    elif parts_n > 5:
        checks.append(f"высокая конкуренция: уже {parts_n} участников")
    if not has_docs:
        checks.append("запросить документы у организатора — сейчас их нет")
    wtc = "; ".join(f"{i + 1}. {c}" for i, c in enumerate(checks[:5]))
    wtc = enrich_what_to_check(wtc, cadastral, "", vin, lot_type)
    if market_known and disc_pct > 0:
        strat = (f"Дисконт {disc_pct}% к рынку. "
                 f"{'Нет заявок. ' if parts_n == 0 else f'{parts_n} участников. '}"
                 f"{'⚠️ Большой дисконт — проверьте причину.' if disc_pct >= 50 else 'Проверьте документы.'}")
    elif not market_known:
        strat = "Рынок не определён — сравните цену с аналогами вручную перед решением."
    else:
        strat = "Проверьте документы и условия торгов."
    return {
        "legal_summary": "",
        "encumbrances": "",
        "owners_count": "?",
        "has_hidden_risks": disc_pct >= 50,
        "legal_risks": ["документы не получены"],
        "invest_risks": ([f"дисконт {disc_pct}% требует объяснения"] if market_known and disc_pct >= 40 else ["стандартные риски"]),
        "invest_opportunities": opps or ["требует анализа"],
        "invest_potential": "высокий" if market_known and disc_pct >= 40 else "средний",
        "risk_level": rl,
        "liquidity_level": "средняя",
        "liquidity_days": 90,
        "exit_strategy": "",
        "strategy": strat,
        "what_to_check": wtc,
        "action": action,
        "verdict": action,
    }


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
            groq_timeout = float(os.getenv("GROQ_TIMEOUT", "18"))
            async with httpx.AsyncClient(timeout=groq_timeout + 2, trust_env=False) as client:
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

    return expert_fallback(
        title, lot_type, lot_price, market_price, disc_pct, parts_n,
        score, cadastral, vin, pdf_text, has_docs=has_docs,
    )


async def analyze_lot(lot: dict, light: bool = False) -> dict:
    title     = lot.get("title_full") or lot.get("title", "")
    region    = lot.get("region", "moskva")
    egrn_pdf  = lot.get("egrn_pdf_text", "") if lot.get("pdf_from_egrn") else ""
    pdf_text  = egrn_pdf
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
    rname     = REGION_LABELS.get(region, region)
    full_text_src = f"{title} {page_text}"
    auto_summary = ""

    if lot_type == "авто":
        auto = parse_auto_meta(f"{title} {page_text} {egrn_pdf}")
        lot.update({k: v for k, v in auto.items() if v})
        if auto.get("vin"):
            vin = auto["vin"]
        ap = []
        if vin:
            ap.append(f"VIN {vin}")
        if auto.get("year"):
            ap.append(f"год {auto['year']}")
        if auto.get("mileage"):
            ap.append(auto["mileage"])
        if auto.get("brand_hint"):
            ap.append(auto["brand_hint"])
        auto_summary = " | ".join(ap)
        red_flags = []
    elif lot_type == "земля":
        land = parse_land_meta(full_text_src)
        lot.update(land)
        red_flags = scan_red_flags(full_text_src, lot_type)
        red_flags = list(dict.fromkeys(red_flags + lot.get("land_flags", [])))
    else:
        red_flags = scan_red_flags(full_text_src, lot_type) if is_real_estate(lot_type) else []

    if lot_price == 0:
        src = egrn_pdf or page_text
        for pat in [r'(\d[\d\s]{4,})\s*(?:руб|₽)', r'цена[^\d]*(\d[\d\s]{4,})']:
            m = re.search(pat, src, re.IGNORECASE)
            if m:
                try:
                    p = float(re.sub(r'\s', '', m.group(1)))
                    if 50_000 < p < 5_000_000_000:
                        lot_price = p
                        break
                except Exception:
                    pass

    if lot_type == "авто":
        mkt = {
            "market_price": 0, "rental_monthly": 0, "price_per_sqm": 0,
            "area": 0, "comment": "оценить рынок авто вручную",
            "manual_market": True,
        }
    else:
        src_area = f"{title} {page_text} {egrn_pdf}"
        mkt = calc_market_price(lot_type, title, region, src_area)
    mkt_prc = mkt["market_price"]
    market_known = not mkt.get("manual_market") and mkt_prc > 0

    disc_pct = 0
    if market_known and lot_price > 0 and mkt_prc > lot_price:
        disc_pct = round((mkt_prc - lot_price) / mkt_prc * 100)
        if disc_pct >= 85:
            disc_pct = 0
            mkt_prc = 0
            market_known = False

    has_pdf = lot.get("pdf_from_egrn") and len(egrn_pdf) > 100
    document_status = resolve_document_status(lot)
    egrn_parsed = lot.get("egrn_parsed") or {}

    if lot_type == "авто":
        legal_text = ""
        encumb_from_egrn = ""
    elif egrn_parsed.get("summary"):
        legal_text = egrn_parsed["summary"]
        encumb_from_egrn = egrn_parsed.get("encumbrances", "")
    elif has_pdf:
        legal_text = "ЕГРН получен — ключевые поля не распознаны автоматически"
        encumb_from_egrn = ""
    else:
        legal_text = ""
        encumb_from_egrn = ""

    score = calc_score(lot_price, mkt_prc if market_known else 0, parts_n, cadastral,
                       step_cur, step_tot, has_pdf, red_flags,
                       lot.get("application_deadline", ""), disc_pct if market_known else 0)
    trading_summary = build_trading_summary(lot)
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
    full_text = egrn_pdf or page_text[:1000]
    has_docs = len(full_text) > 100 or len(analytics) > 50
    if light:
        expert = expert_fallback(
            title, lot_type, lot_price, mkt_prc if market_known else 0,
            disc_pct if market_known else 0, parts_n, score,
            cadastral if is_real_estate(lot_type) else "", vin, full_text,
            has_docs=has_docs,
        )
    else:
        groq_timeout = float(os.getenv("GROQ_TIMEOUT", "18"))
        try:
            expert = await asyncio.wait_for(
                get_expert_analysis(
                    title, lot_type, rname, lot_price, mkt_prc if market_known else 0,
                    disc_pct if market_known else 0, 0, parts_n, step_info,
                    cadastral if is_real_estate(lot_type) else "", vin,
                    full_text, analytics, rosreestr if is_real_estate(lot_type) else "", score,
                ),
                timeout=groq_timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Groq timeout %.0fs для лота %s", groq_timeout, lot.get("id", ""))
            expert = expert_fallback(
                title, lot_type, lot_price, mkt_prc if market_known else 0,
                disc_pct if market_known else 0, parts_n, score,
                cadastral if is_real_estate(lot_type) else "", vin, full_text,
                has_docs=has_docs,
            )

    roi_parts = []
    if mkt.get("manual_market"):
        if lot_type == "авто":
            roi_parts.append("оценить рынок авто вручную")
        elif lot_type == "земля":
            roi_parts.append("оценить рынок земли вручную")
        else:
            roi_parts.append("рынок не определён — оценить вручную")
    elif market_known and disc_pct > 0 and mkt_prc > 0 and lot_price > 0:
        profit = mkt_prc - lot_price
        roi_parts.append(f"ориентир перепродажи +{disc_pct}% (+{profit/1e6:.1f}млн₽)")
    roi_text = " | ".join(roi_parts) if roi_parts else "нет данных"

    def fmt(p, *, is_market=False):
        try:
            p = float(p)
            if p >= 1_000_000:
                return f"{p/1_000_000:.1f} млн ₽"
            if p > 0:
                return f"{int(p):,} ₽".replace(",", " ")
        except (TypeError, ValueError):
            pass
        if is_market:
            return "не определён"
        return "уточните на сайте"

    invest_icons = {"высокий": "🔥", "средний": "📈", "низкий": "📉"}
    risk_icons = {"низкий": "🟢", "средний": "🟡", "высокий": "🟠", "критический": "🔴"}
    ip = expert.get("invest_potential", "средний") or "средний"
    rl_raw = expert.get("risk_level", "средний") or "средний"
    if market_known and disc_pct >= 60 and not has_pdf:
        rl = "высокий"
    elif market_known and disc_pct >= 40 and not has_pdf and rl_raw == "низкий":
        rl = "средний"
    elif market_known and disc_pct >= 60 and has_pdf and rl_raw in ("средний", "высокий"):
        rl = "средний"
    else:
        rl = rl_raw
    action = expert.get("action",
                        "ВХОДИТЬ СЕЙЧАС" if score >= 8 else
                        "ЖДАТЬ СНИЖЕНИЯ" if score >= 7 else "ПРОВЕРИТЬ ДОКУМЕНТЫ")
    risks = expert.get("invest_risks", []) or []
    opps = expert.get("invest_opportunities", []) or []

    extra = []
    if market_known and disc_pct >= 60:
        extra.append(f"⚠️ Дисконт {disc_pct}% — проверьте причину низкой цены!")
    elif market_known and disc_pct >= 40:
        extra.append(f"💡 Дисконт {disc_pct}% — выгодно при чистых документах")
    if is_real_estate(lot_type) and cadastral:
        cad_line = f"🏛 Кадастр: {cadastral}"
        if rosreestr:
            cad_line += f"\n   📍 {rosreestr}"
        extra.append(cad_line)
    if lot_type == "авто" and vin:
        extra.append(f"🔍 VIN: {vin}")
    if parts_n == 0:
        extra.append("👥 Нет заявок" + (" — можно взять по стартовой цене" if market_known else ""))
    elif parts_n == 1:
        extra.append("👥 1 участник — конкуренция низкая")
    elif parts_n <= 3:
        extra.append(f"👥 {parts_n} участника — умеренная конкуренция")
    elif parts_n > 5:
        extra.append(f"👥 ⚠️ {parts_n} участников — высокая конкуренция!")
    valid_risks = [r for r in risks if r not in
                   ("нет данных", "документы не получены — проверьте перед покупкой",
                    "требует проверки", "стандартные риски")]
    valid_opps = [o for o in opps if o not in ("требует анализа",)]
    if market_known and valid_risks:
        extra.append("⚠️ Риски: " + " | ".join(str(r) for r in valid_risks[:2]))
    if market_known and valid_opps:
        extra.append("✨ Плюсы: " + " | ".join(str(o) for o in valid_opps[:2]))
    encumb = expert.get("encumbrances", "") or encumb_from_egrn or ""
    exit_s = expert.get("exit_strategy", "") or ""
    strategy = expert.get("strategy", "")
    if not market_known and "Дисконт" in strategy:
        strategy = "Рынок не определён — сравните цену с аналогами вручную."

    partial_an = {
        "lot_price_raw": lot_price,
        "market_price_raw": mkt_prc if market_known else 0,
        "discount_pct": str(disc_pct) if market_known and disc_pct > 0 else "?",
        "land_manual_market": mkt.get("manual_market", False) or not market_known,
        "market_known": market_known,
    }
    from verdict import run_verdict_pipeline
    vr = run_verdict_pipeline(lot, partial_an)
    verdict_label = vr["verdict_label"]
    action_map_v = {
        "Мимо": "🔴", "Смотреть": "🟡",
        "Брать на due diligence": "🟢",
        "Чисто, но неинтересно по цене": "🟢",
    }
    verdict_emoji = action_map_v.get(verdict_label, "⚠️")

    an_stub = {
        "price": fmt(lot_price),
        "market_price": fmt(mkt_prc, is_market=True) if market_known else "не определён",
        "land_manual_market": mkt.get("manual_market", False),
        "market_known": market_known,
        "discount_pct": str(disc_pct) if market_known and disc_pct > 0 else "?",
        "lot_type": lot_type,
    }

    return {
        "lot_type":       lot_type,
        "total_score":    score,
        "score_label":    f"{'🔥' if score>=9 else '⭐' if score>=8 else '📊'} {score}/10",
        "price":          an_stub["price"],
        "market_price":   an_stub["market_price"],
        "market_known":   market_known,
        "market_comment": mkt.get("comment", "") if not market_known else mkt.get("comment", ""),
        "discount_pct":   an_stub["discount_pct"],
        "lot_price_raw":  lot_price,
        "market_price_raw": mkt_prc if market_known else 0,
        "price_line":     format_price_line(an_stub),
        "step":           step_info,
        "liquidity_text": f"{expert.get('liquidity_level','средняя')} (~{expert.get('liquidity_days',90)} дней)",
        "roi_text":       roi_text,
        "legal_text":     legal_text,
        "document_status": document_status,
        "auto_summary":   auto_summary,
        "encumbrances":   encumb if encumb not in ("уточните на сайте", "") else "",
        "owners":         egrn_parsed.get("owner") or expert.get("owners_count", "?"),
        "exit_strategy":  exit_s if exit_s not in ("уточните после проверки документов", "") else "",
        "extra_checks":   "\n".join(extra),
        "risk_text":      f"{risk_icons.get(rl,'🟡')} риск: {rl}",
        "invest_text":    f"{invest_icons.get(ip,'📈')} потенциал: {ip}",
        "strategy":       strategy,
        "what_to_check":  enrich_what_to_check(
            expert.get("what_to_check", ""), cadastral, address, vin, lot_type),
        "action":         action,
        "action_emoji":   verdict_emoji,
        "verdict":        expert.get("verdict", action),
        "has_pdf":        has_pdf,
        "urgency":        urgency,
        "verdict_simple": verdict_label,
        "worth_showing":  True,
        "trading_summary": trading_summary,
        "red_flags_text": ", ".join(red_flags[:5]) if red_flags else "",
        "land_manual_market": mkt.get("manual_market", False),
        "dedup_note": lot.get("dedup_note", ""),
        "facts_json": vr["facts_json"],
        "risk_score": vr["risk_score"],
        "risk_level": vr["risk_level"],
        "invest_score": vr["invest_score"],
        "verdict_label": verdict_label,
        "verdict_detail": vr["verdict_detail"],
        "verdict_card": vr["verdict_card"],
        "manual_checks": vr["manual_checks"],
    }
