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
MIN_DISCOUNT_PCT = float(os.getenv("MIN_DISCOUNT_PCT", "15"))
HOT_LABEL_PCT = float(os.getenv("HOT_LABEL_PCT", "10"))  # пометка 🔥, не фильтр дайджеста
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "10"))

DIGEST_FORMULA = "45%×дисконт + 35%×низкая конкуренция + 20%×чистые документы"
TG_MSG_LIMIT = 4096
TG_CALLBACK_LIMIT = 64

_URL_ENC_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_LONG_URL_RE = re.compile(r"https?://\S{80,}")


def _clean_visible_text(text: str, max_len: int = 300) -> str:
    """Убирает сырой URL-код и лишние пробелы из текста карточки."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if _URL_ENC_RE.search(s):
        s = re.sub(r"\S*%[0-9A-Fa-f]{2}(?:%[0-9A-Fa-f]{2})+\S*", "", s)
        s = re.sub(r"\s+", " ", s).strip()
    s = _LONG_URL_RE.sub("[ссылка]", s)
    return s[:max_len]


def _tg_escape_md(text: str) -> str:
    """Экранирование для Telegram legacy Markdown."""
    s = str(text or "")
    for ch in ("\\", "_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def _strip_md_markers(text: str) -> str:
    s = re.sub(r"_([^_]+)_", r"\1", str(text or ""))
    return s.replace("*", "")


def clamp_telegram_message(text: str, limit: int = TG_MSG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n\n… _(обрезано)_"


def format_short_lot_message_plain(lot: dict, an: dict, label: str = "ЛОТ") -> str:
    md = format_short_lot_message(lot, an, label)
    return clamp_telegram_message(_strip_md_markers(md.replace("\\", "")))


def format_minimal_lot_card(lot: dict, an: dict, label: str = "ЛОТ") -> str:
    lot_id = lot.get("id", "?")
    disc = an.get("discount_pct", "?")
    url = lot.get("url") or f"https://tbankrot.ru/item?id={lot_id}"
    return clamp_telegram_message(
        f"{label} — лот {lot_id}\n"
        f"{_clean_visible_text(lot.get('title', ''), 80)}\n"
        f"Дисконт: {disc}% | рейтинг: {an.get('digest_rating', '—')}\n"
        f"{url}"
    )

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
    """Строка сроков/стадии + метрики из аналитики торгов."""
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
    ta = lot.get("trading_analytics") or {}
    appr = lot.get("appraisal_parsed") or {}
    red = ta.get("avg_reduction_pct") or appr.get("auction_avg_reduction_pct", "")
    apps = ta.get("participants_hint") or appr.get("auction_participants_hint", "")
    if red and red not in ("", "не указано"):
        val = red.replace(" (аналитика торгов)", "").replace("(аналитика торгов)", "")
        parts.append(f"среднее снижение на публичке {val}")
    if apps and apps not in ("", "не указано"):
        parts.append(f"среднее заявок {apps}")
    return " | ".join(parts)


def _resolve_participants(lot: dict) -> int | None:
    """Число заявок: карточка лота или среднее из аналитики торгов."""
    p = lot.get("participants")
    if p is not None and str(p).strip() != "":
        try:
            v = int(float(p))
            if 0 <= v <= 500:
                return v
        except (TypeError, ValueError):
            pass
    ta = lot.get("trading_analytics") or {}
    appr = lot.get("appraisal_parsed") or {}
    hint = ta.get("participants_hint") or appr.get("auction_participants_hint", "")
    if hint and str(hint) not in ("", "не указано"):
        try:
            return int(round(float(str(hint).replace(",", "."))))
        except ValueError:
            pass
    return None


def competition_level(participants: int | None) -> str:
    if participants is None:
        return "неизвестна"
    if participants <= 2:
        return "низкая"
    if participants <= 5:
        return "средняя"
    return "высокая"


def _documents_clean(lot: dict, an: dict) -> bool:
    for rec in lot.get("egrn_records") or []:
        if rec.get("encumbrances_clean"):
            continue
        enc = (rec.get("encumbrances") or "").strip().lower()
        if enc and enc not in ("нет", "не зарегистрировано", "не указано", "отсутствуют"):
            return False
    egrn = lot.get("egrn_parsed") or {}
    if egrn.get("encumbrances_clean"):
        return True
    enc = (an.get("encumbrances") or "").strip().lower()
    if not enc:
        return True
    if enc in ("нет", "не зарегистрировано", "не указано", "отсутствуют"):
        return True
    return False


def compute_digest_metrics(lot: dict, an: dict) -> dict:
    """
    Итоговый рейтинг для дайджеста (0–100):
    45% — дисконт % (выше лучше)
    35% — низкая конкуренция (меньше заявок — лучше)
    20% — чистота документов (нет обременений — лучше)
    """
    disc_raw = an.get("discount_pct", "?")
    try:
        disc = float(disc_raw) if str(disc_raw) not in ("?", "", "0") else 0.0
    except (TypeError, ValueError):
        disc = 0.0
    market_known = bool(an.get("market_known"))

    if market_known and disc > 0:
        disc_score = min(100.0, disc * 3.0)
    elif disc > 0:
        disc_score = min(60.0, disc * 2.0)
    else:
        disc_score = 0.0

    pcount = _resolve_participants(lot)
    comp_lvl = competition_level(pcount)
    if pcount is None:
        comp_score = 50.0
    elif pcount == 0:
        comp_score = 100.0
    elif pcount <= 2:
        comp_score = 85.0
    elif pcount <= 5:
        comp_score = 55.0
    elif pcount <= 10:
        comp_score = 30.0
    else:
        comp_score = 10.0

    doc_clean = _documents_clean(lot, an)
    doc_score = 100.0 if doc_clean else 35.0

    rating = round(disc_score * 0.45 + comp_score * 0.35 + doc_score * 0.20, 1)
    hot_label = bool(market_known and disc >= HOT_LABEL_PCT)

    if hot_label and comp_lvl == "низкая":
        section = "best"
    elif disc >= 5 and comp_lvl in ("средняя", "высокая"):
        section = "competitive"
    elif doc_clean:
        section = "clean"
    else:
        section = "other"

    parts_disp = str(pcount) if pcount is not None else "н/д"
    return {
        "digest_rating": rating,
        "competition_level": comp_lvl,
        "participants_count": pcount,
        "participants_display": parts_disp,
        "documents_clean": doc_clean,
        "documents_label": "нет" if doc_clean else "есть",
        "hot_label": hot_label,
        "digest_section": section,
    }


def enrich_digest_metrics(lot: dict, an: dict) -> dict:
    """Добавляет поля рейтинга дайджеста к результату анализа."""
    metrics = compute_digest_metrics(lot, an)
    return {**an, **metrics}


def format_short_lot_message(lot: dict, an: dict, label: str = "ЛОТ") -> str:
    """Короткая карточка: балл, цена, дисконт, конкуренция, обременения, ссылка."""
    score = an.get("total_score", "?")
    verdict = _clean_visible_text(
        an.get("verdict_label") or an.get("verdict_simple") or an.get("action", "?"), 120,
    )
    title = _clean_visible_text(lot.get("title", ""), 70)
    rn = f" 🌍 {_clean_visible_text(lot.get('region', ''), 30)}" if lot.get("is_extra") else ""
    hot = "🔥 " if an.get("hot_label") else ""
    label_safe = _clean_visible_text(label, 40)
    lines = [
        f"🔔 *{hot}{_tg_escape_md(label_safe)} — {score}/10*{rn}",
        _tg_escape_md(title),
    ]
    if an.get("dedup_note"):
        lines.append(f"_{_tg_escape_md(_clean_visible_text(an['dedup_note'], 120))}_")
    pl = an.get("price_line") or format_price_line(an)
    pl = _clean_visible_text(_strip_md_markers(pl), 420)
    lines.append(_tg_escape_md(pl))

    disc = an.get("discount_pct", "?")
    disc_s = f"{disc}%" if str(disc) not in ("?", "", "0") else "не определён"
    parts_d = an.get("participants_display")
    if not parts_d:
        p = _resolve_participants(lot)
        parts_d = str(p) if p is not None else "н/д"
    comp = an.get("competition_level") or competition_level(
        an.get("participants_count") if an.get("participants_count") is not None
        else _resolve_participants(lot)
    )
    doc_l = an.get("documents_label")
    if not doc_l:
        doc_l = "нет" if _documents_clean(lot, an) else "есть"
    lines.append(f"📉 Дисконт: *{disc_s}* · 👥 заявок: *{parts_d}* ({comp})")
    enc_line = f"📄 Обременения: *{doc_l}*"
    if doc_l == "есть" and an.get("encumbrances"):
        enc_line += f" — {_tg_escape_md(_clean_visible_text(an['encumbrances'], 70))}"
    lines.append(enc_line)
    dr = an.get("digest_rating")
    if dr is not None:
        lines.append(f"⭐ Рейтинг: *{dr}/100*")

    if an.get("lot_type") == "авто" and an.get("auto_summary"):
        lines.append(f"🚗 {_tg_escape_md(_clean_visible_text(an['auto_summary'], 100))}")
    lines += [
        f"{an.get('action_emoji', '📋')} *{_tg_escape_md(verdict)}*",
        f"🔗 {_clean_visible_text(lot.get('url', ''), 200)}",
    ]
    return clamp_telegram_message("\n".join(lines))


def deep_callback_data(lot_id: str, an: dict, lot: dict, parsed_at=None) -> str:
    """callback_data кнопки «Полный анализ» (≤64 байт для Telegram)."""
    if parsed_at is None:
        ts = int(time.time())
    elif isinstance(parsed_at, datetime):
        ts = int(parsed_at.timestamp())
    else:
        ts = int(parsed_at)
    disc = str(an.get("discount_pct", "0") or "0").replace(".", "")[:6]
    full = (
        f"deep_{lot_id}_{int(an.get('lot_price_raw', 0) or 0)}_"
        f"{int(an.get('market_price_raw', 0) or 0)}_{disc}_"
        f"{lot.get('participants', 0)}_{ts}"
    )
    if len(full.encode("utf-8")) <= TG_CALLBACK_LIMIT:
        return full
    short = f"deep_{lot_id}_{ts}"
    if len(short.encode("utf-8")) <= TG_CALLBACK_LIMIT:
        return short
    return f"deep_{lot_id}"[:TG_CALLBACK_LIMIT]


def lot_action_keyboard(lot_id: str, an: dict, lot: dict, parsed_at=None):
    """Кнопки под лотом: Полный анализ + Сохранить."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    cb = deep_callback_data(lot_id, an, lot, parsed_at)
    save_cb = f"save_{lot_id}"
    if len(save_cb.encode("utf-8")) > TG_CALLBACK_LIMIT:
        save_cb = save_cb[:TG_CALLBACK_LIMIT]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Полный анализ", callback_data=cb),
        InlineKeyboardButton("⭐ Сохранить", callback_data=save_cb),
    ]])


def is_real_estate(lot_type: str) -> bool:
    return lot_type in ("квартира", "апартаменты", "дом", "коммерция", "земля", "гараж")


_ENC_GARBAGE_RE = re.compile(
    r"аукцион|завершен|открытый\s+аук|аренда\s+нежил|"
    r"\d[\d\s]{2,}[,.]\d{2}\s*(?:₽|руб)?|"
    r"кадастров[^\n]{0,15}номер|\bкв\.?\s*м\b",
    re.I,
)

_ENC_KNOWN = (
    (r"ипотека\s+в\s+силу\s+закона", "Ипотека в силу закона"),
    (r"запрещени[^\n]{0,30}регистрац", "Запрещение регистрации"),
    (r"ограничени[яе]\s+прав[^\n]{0,50}стать[^\n]{0,20}\d+", None),
    (r"\bарест\b", "Арест"),
    (r"\bзалог\b", "Залог"),
    (r"\bсервитут\b", "Сервитут"),
)


def _clean_field_text(text: str, max_len: int = 100) -> str:
    if not text:
        return ""
    s = re.sub(r"\s{2,}", " ", str(text).strip())
    s = re.sub(r"^\(обременения\)\s*:\s*", "", s, flags=re.I)
    s = re.sub(r"^вид\s+(?:обременени|ограничени)[яь]\s*:\s*", "", s, flags=re.I)
    if _ENC_GARBAGE_RE.search(s):
        return ""
    return s[:max_len].strip(" ;,")


def _egrn_encumbrance_sections(norm: str) -> str:
    chunks: list[str] = []
    for pat in (
        r"Сведения о зарегистрир[^\n]*обременен[^\n]*\n([\s\S]{5,1500}?)"
        r"(?:\n\s*\d+[.)]\s|\nСведения |\Z)",
        r"Сведения об ограничениях прав[^\n]*\n([\s\S]{5,1500}?)"
        r"(?:\n\s*\d+[.)]\s|\nСведения |\Z)",
    ):
        for m in re.finditer(pat, norm, re.I):
            chunks.append(m.group(1))
    return "\n".join(chunks)


def _summarize_egrn_encumbrances(lot: dict) -> str:
    records = dedupe_egrn_records(list(lot.get("egrn_records") or []))
    if not records:
        egrn = lot.get("egrn_parsed") or {}
        return _clean_field_text(egrn.get("encumbrances") or "", 100)
    parts: list[str] = []
    for rec in records:
        enc = _clean_field_text(rec.get("encumbrances") or "", 100)
        if not enc or enc == "не указано в распознанном тексте":
            continue
        cad = rec.get("cadastral") or "?"
        parts.append(f"{cad}: {enc}" if enc != "обременений не зарегистрировано" else f"{cad}: нет")
    return "; ".join(parts)[:120]


def _extract_encumbrances_detail(norm: str) -> tuple[str, bool]:
    """Короткая формулировка обременений — только из раздела ЕГРН."""
    enc_blob = _egrn_encumbrance_sections(norm)
    check = enc_blob if enc_blob.strip() else norm[:5000]
    ctl = check.lower()

    if re.search(
        r"нет\s+зарегистрированных\s+обремен|"
        r"обременени[яе][^\n]{0,40}не\s+зарегистрир|"
        r"сведения\s+об\s+отсутствии\s+обремен|"
        r"не\s*зарегистрировано",
        ctl,
    ):
        if not re.search(r"ограничени[^\n]{0,80}стать[^\n]{0,30}\d", ctl):
            if not re.search(
                r"(?:вид|тип)\s*(?:обременени|ограничени)[^\n]{8,}", check, re.I,
            ):
                return "обременений не зарегистрировано", True

    found: list[str] = []

    def _add(phrase: str) -> None:
        p = _clean_field_text(phrase, 100)
        if p and len(p) >= 3 and p.lower() not in {x.lower() for x in found}:
            found.append(p)

    search = enc_blob if enc_blob.strip() else ""
    if search:
        for pat, label in _ENC_KNOWN:
            m = re.search(pat, search, re.I)
            if m:
                _add(label if label else m.group(0))
        for m in re.finditer(
            r"(?:вид\s+обременени[яь]|вид\s+ограничени[яь])[:\s]+([^\n]{3,90})",
            search, re.I,
        ):
            _add(m.group(1))
    else:
        for m in re.finditer(
            r"(?:вид\s+обременени[яь]|вид\s+ограничени[яь])[:\s]+([^\n]{3,90})",
            norm[:8000], re.I,
        ):
            _add(m.group(1))
        if re.search(r"ипотека\s+в\s+силу\s+закона", norm[:8000], re.I):
            _add("Ипотека в силу закона")

    if found:
        return ", ".join(found[:4])[:100], False

    if enc_blob.strip() and re.search(r"ипотек|залог|арест|сервитут|запрещени", ctl):
        return "обременение указано — проверить выписку", False

    return "не указано в распознанном тексте", False


def parse_egrn_pdf(text: str) -> dict:
    """Извлекает поля из текста выписки ЕГРН (PDF/OCR)."""
    result = {
        "cadastral": "", "address": "", "area": "",
        "owner": "", "encumbrances": "", "share": "", "arrests": "",
        "encumbrances_clean": False,
        "cadastral_value": 0,
        "summary": "", "parsed_ok": False,
    }
    if not text or len(text) < 80:
        return result
    # нормализация OCR-артефактов
    norm = re.sub(r"[ \t]+", " ", text)
    norm = re.sub(r"\n{3,}", "\n\n", norm)

    kad = re.search(r"\b(\d{2}:\d{2}:\d{6,7}:\d+)\b", norm)
    if kad:
        result["cadastral"] = kad.group(1)

    for pat in [
        r"(?:местоположени[ея]|адрес(?:\(местоположение\))?)[:;\s]+([^\n]{15,220})",
        r"(?:находится по адресу)[:;\s]+([^\n]{15,220})",
        r"Адрес[:\s]+([^\n]{15,220})",
    ]:
        m = re.search(pat, norm, re.IGNORECASE)
        if m:
            addr = re.sub(r"\s{2,}", " ", m.group(1).strip())
            if len(addr) > 12:
                result["address"] = addr[:180]
                break

    for pat in [
        r"площад[ьи][^\d]{0,40}(\d+[.,]?\d*)\s*(?:кв\.?\s*м|м²|кв\.м|кв\.?\s*м\.)",
        r"(\d+[.,]?\d*)\s*(?:кв\.?\s*м|м²)\s*[\n—-]",
    ]:
        am = re.search(pat, norm, re.IGNORECASE)
        if am:
            result["area"] = am.group(1).replace(",", ".") + " м²"
            break

    # правообладатель / собственник
    owner_block = ""
    for pat in [
        r"Сведения о правообладател[^\n]*\n([\s\S]{20,800}?)(?:\n\s*\d+\.\s|\nСведения о)",
        r"правообладател(?:ь|и)[:\s]+([^\n]{5,160})",
        r"собственник[:\s]+([^\n]{5,160})",
    ]:
        m = re.search(pat, norm, re.IGNORECASE)
        if m:
            owner_block = m.group(1).strip()
            break
    if owner_block:
        # убираем тип записи, оставляем ФИО/наименование
        lines = [ln.strip() for ln in owner_block.split("\n") if ln.strip()]
        for ln in lines:
            if re.search(r"(физическ|юридическ|гражданин|общество|банк|российской федерации)", ln, re.I):
                continue
            if len(ln) > 5 and not re.fullmatch(r"[\d\s:.-]+", ln):
                result["owner"] = ln[:120]
                break
        if not result["owner"] and lines:
            result["owner"] = lines[0][:120]

    # доля
    sm = re.search(
        r"размер доли[:\s]+([^\n]{3,80})|"
        r"(\d+/\d+)\s*(?:в праве|дол)",
        norm, re.I,
    )
    if sm:
        result["share"] = (sm.group(1) or sm.group(2) or "").strip()[:80]

    # обременения / аресты — конкретная формулировка из текста
    enc_detail, enc_clean = _extract_encumbrances_detail(norm)
    result["encumbrances"] = enc_detail
    result["encumbrances_clean"] = enc_clean

    if re.search(r"\barrest\b|арест\s|наложен\s+арест|запрещени[ея]\s+регистрац", norm, re.I):
        am = re.search(r"(арест[^\n]{5,120}|запрещени[ея][^\n]{5,120})", norm, re.I)
        result["arrests"] = (am.group(1).strip()[:120] if am else "упоминается в тексте ЕГРН")

    cv_m = re.search(
        r"кадастров[\s\S]{0,70}?стоим[^\d]{0,25}(\d[\d\s]{5,11})",
        norm, re.I,
    )
    if cv_m:
        try:
            cv = int(re.sub(r"\s", "", cv_m.group(1)))
            if 50_000 <= cv <= 2_000_000_000:
                result["cadastral_value"] = cv
        except ValueError:
            pass

    parts = []
    if result["cadastral"]:
        parts.append(f"кадастр {result['cadastral']}")
    if result["address"]:
        parts.append(result["address"][:80])
    if result["area"]:
        parts.append(result["area"])
    if result["owner"]:
        parts.append(f"собственник: {result['owner'][:60]}")
    if result["share"]:
        parts.append(f"доля: {result['share'][:40]}")
    if result["encumbrances"]:
        parts.append(f"обременения: {result['encumbrances'][:70]}")
    if result["arrests"]:
        parts.append(f"аресты: {result['arrests'][:50]}")
    result["summary"] = " | ".join(parts)
    result["parsed_ok"] = bool(
        result["cadastral"] or result["owner"] or result["encumbrances"] or result["address"]
    )
    return result


def format_egrn_legal_block(egrn: dict) -> str:
    """Читаемый блок юридических данных из ЕГРН (один объект или несколько)."""
    if not egrn:
        return ""
    objects = dedupe_egrn_records(egrn.get("objects") or [])
    if objects:
        lines = []
        for i, obj in enumerate(objects, 1):
            block = _format_single_egrn_block(obj)
            if block:
                title = obj.get("source_title") or f"Объект {i}"
                lines.append(f"【{title}】")
                lines.append(block)
        return "\n".join(lines)
    return _format_single_egrn_block(egrn)


def _format_single_egrn_block(egrn: dict) -> str:
    if not egrn:
        return ""
    lines = []
    if egrn.get("cadastral"):
        lines.append(f"Кадастр: {egrn['cadastral']}")
    if egrn.get("address"):
        lines.append(f"Адрес: {egrn['address'][:100]}")
    if egrn.get("area"):
        lines.append(f"Площадь: {egrn['area']}")
    if egrn.get("owner"):
        lines.append(f"Собственник: {egrn['owner'][:100]}")
    if egrn.get("share"):
        lines.append(f"Доля: {egrn['share']}")
    if egrn.get("encumbrances"):
        lines.append(f"Обременения: {_clean_field_text(egrn['encumbrances'], 100)}")
    if egrn.get("arrests"):
        lines.append(f"Аресты: {egrn['arrests'][:120]}")
    return "\n".join(lines)


def apply_appraisal_to_lot(lot: dict, pdf_text: str, method: str = "", source_title: str = "") -> None:
    """Парсит отчёт об оценке и дополняет lot."""
    try:
        from appraisal_pdf import parse_appraisal_pdf
    except ImportError:
        log.warning("appraisal_pdf unavailable")
        return
    try:
        lot["pdf_from_appraisal"] = True
        lot["appraisal_pdf_text"] = pdf_text
        parsed = parse_appraisal_pdf(pdf_text)
        if method:
            parsed["extract_method"] = method
        if source_title:
            parsed["source_title"] = source_title
        lot["appraisal_parsed"] = parsed
        if parsed.get("parsed_ok"):
            lot["appraisal_read_ok"] = True
        if parsed.get("restriction_flags"):
            lot["appraisal_flags"] = parsed["restriction_flags"]
    except Exception:
        log.exception("apply_appraisal_to_lot failed for lot %s", lot.get("id", ""))


def _normalize_cadastral(cad: str) -> str:
    return re.sub(r"\s+", "", str(cad or "").strip())


def _egrn_record_quality(rec: dict) -> int:
    score = 0
    if rec.get("parsed_ok"):
        score += 100
    if rec.get("encumbrances_clean"):
        score += 20
    for key in ("encumbrances", "address", "owner", "area"):
        score += min(len(str(rec.get(key) or "")), 40)
    return score


def _merge_egrn_duplicate(primary: dict, other: dict) -> dict:
    """Сливает две выписки на один кадастр — оставляет более полную."""
    keep = primary if _egrn_record_quality(primary) >= _egrn_record_quality(other) else other
    drop = other if keep is primary else primary
    merged = dict(keep)
    for key in ("address", "area", "owner", "encumbrances", "share", "arrests", "summary"):
        if not merged.get(key) and drop.get(key):
            merged[key] = drop[key]
    if drop.get("parsed_ok"):
        merged["parsed_ok"] = True
    if drop.get("encumbrances_clean"):
        merged["encumbrances_clean"] = True
    return merged


def dedupe_egrn_records(records: list[dict]) -> list[dict]:
    """Один кадастровый номер = один объект (дубли выписки сливаются)."""
    by_cad: dict[str, dict] = {}
    order: list[str] = []
    no_cad: list[dict] = []
    for rec in records or []:
        if not rec:
            continue
        cad = _normalize_cadastral(rec.get("cadastral") or "")
        if not cad:
            no_cad.append(rec)
            continue
        if cad not in by_cad:
            by_cad[cad] = dict(rec)
            order.append(cad)
        else:
            by_cad[cad] = _merge_egrn_duplicate(by_cad[cad], rec)
    return [by_cad[c] for c in order] + no_cad


def _merge_egrn_records(records: list[dict]) -> dict:
    """Сводка по нескольким выпискам ЕГРН."""
    records = dedupe_egrn_records(records)
    merged = {
        "objects": records,
        "cadastral": "", "address": "", "area": "",
        "owner": "", "encumbrances": "", "share": "", "arrests": "",
        "summary": "", "parsed_ok": False,
    }
    summaries = []
    for rec in records:
        if rec.get("parsed_ok"):
            merged["parsed_ok"] = True
        if rec.get("summary"):
            summaries.append(rec["summary"])
        if not merged["cadastral"] and rec.get("cadastral"):
            merged["cadastral"] = rec["cadastral"]
        if not merged["address"] and rec.get("address"):
            merged["address"] = rec["address"]
        if not merged["area"] and rec.get("area"):
            merged["area"] = rec["area"]
        if not merged["owner"] and rec.get("owner"):
            merged["owner"] = rec["owner"]
        if not merged["encumbrances"] and rec.get("encumbrances"):
            merged["encumbrances"] = rec["encumbrances"]
    merged["summary"] = " || ".join(summaries)
    return merged


def apply_egrn_to_lot(
    lot: dict, pdf_text: str, from_real_pdf: bool,
    source_title: str = "", method: str = "",
) -> None:
    """Парсит выписку ЕГРН и добавляет к списку объектов лота."""
    lot["pdf_from_egrn"] = lot.get("pdf_from_egrn") or from_real_pdf
    if from_real_pdf and pdf_text:
        prev = lot.get("egrn_pdf_text") or ""
        lot["egrn_pdf_text"] = pdf_text if len(pdf_text) > len(prev) else prev
        lot["pdf_text"] = lot["egrn_pdf_text"]
    egrn = parse_egrn_pdf(pdf_text) if from_real_pdf and pdf_text else {}
    if source_title:
        egrn["source_title"] = source_title
    if method:
        egrn["extract_method"] = method

    records = list(lot.get("egrn_records") or [])
    records.append(egrn)
    records = dedupe_egrn_records(records)
    lot["egrn_records"] = records
    lot["egrn_parsed"] = _merge_egrn_records(records)

    if from_real_pdf and egrn.get("parsed_ok"):
        lot["egrn_read_ok"] = True
    if method:
        lot["egrn_extract_method"] = method
    if egrn.get("cadastral") and not lot.get("cadastral"):
        lot["cadastral"] = egrn["cadastral"]
    if egrn.get("address") and not lot.get("address"):
        lot["address"] = egrn["address"]
    if egrn.get("area") and not lot.get("area_sqm"):
        try:
            lot["area_sqm"] = float(egrn["area"].replace(" м²", "").replace(",", "."))
        except ValueError:
            pass


def apply_lot_document(
    lot: dict, doc_type: str, text: str, parsed: dict,
    title: str = "", method: str = "",
) -> None:
    """Применяет результат разбора одного документа к лоту."""
    if doc_type == "egrn":
        apply_egrn_to_lot(lot, text, True, source_title=title, method=method)
    elif doc_type == "appraisal":
        apply_appraisal_to_lot(lot, text, method=method, source_title=title)
        if method:
            lot["appraisal_extract_method"] = method
    elif doc_type == "contract":
        lot["contract_parsed"] = parsed
    elif doc_type == "application":
        lot["application_parsed"] = parsed
    elif doc_type == "info_message":
        lot["info_message_parsed"] = parsed


def resolve_document_status(lot: dict) -> str:
    """Единый статус документов — без выдумок."""
    egrn = lot.get("egrn_parsed") or {}
    appr = lot.get("appraisal_parsed") or {}
    docs = lot.get("lot_documents") or lot.get("pdfs_downloaded") or []
    n_total = lot.get("documents_downloaded_count") or len([d for d in docs if d.get("download_ok")])
    n_egrn = len(dedupe_egrn_records(lot.get("egrn_records") or egrn.get("objects") or []))
    parts = []

    if lot.get("egrn_read_ok") or (lot.get("pdf_from_egrn") and egrn.get("parsed_ok")):
        suffix = " (OCR)" if lot.get("egrn_extract_method") == "ocr" else ""
        if n_egrn > 1:
            parts.append(f"ЕГРН ×{n_egrn}{suffix}")
        else:
            parts.append(f"ЕГРН{suffix}")
    elif lot.get("egrn_ocr_failed"):
        parts.append("ЕГРН не распознан")
    if lot.get("appraisal_read_ok") or appr.get("parsed_ok"):
        parts.append("отчёт об оценке")
    if lot.get("contract_parsed", {}).get("parsed_ok"):
        parts.append("договор")
    if lot.get("application_parsed", {}).get("parsed_ok"):
        parts.append("заявка")
    if lot.get("info_message_parsed", {}).get("parsed_ok"):
        parts.append("инф.сообщение")
    if lot.get("has_photos"):
        parts.append("фото")

    if parts:
        base = "Документы: " + ", ".join(parts)
        if n_total > len(parts):
            base += f" (всего файлов: {n_total})"
        return base
    if lot.get("pdf_download_failed"):
        if lot.get("has_documents_on_site") or lot.get("has_egrn_on_site"):
            return "документы на сайте — не удалось скачать"
        return "документы не получены"
    if lot.get("has_documents_on_site") or lot.get("has_egrn_on_site"):
        return "документы на сайте — текст не извлечён"
    if n_total:
        return f"скачано файлов: {n_total}, ключевые поля не распознаны"
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
    """Кликабельные ссылки для ручной проверки (без сырого URL в тексте)."""
    if lot_type == "авто":
        return build_auto_verification_links(vin)
    lines = []
    kad = (cadastral or "").strip()
    addr = (address or "").strip()
    if kad:
        q = quote(kad, safe="")
        lines.append(f"[Кадастровая карта](https://nspd.gov.ru/map?thematic=PKK&query={q})")
        lines.append(f"[Росреестр ПКК](https://pkk.rosreestr.ru/#/search?text={q})")
    else:
        lines.append("Кадастровый номер не найден — см. карточку лота или ЕГРН")
    lines.append("[ФССП — банк данных](https://fssp.gov.ru/iss/ip)")
    if addr:
        lines.append(f"📍 Адрес для поиска: {addr[:80]}")
    return "\n".join(lines)


def enrich_what_to_check(wtc: str, cadastral: str, address: str = "",
                         vin: str = "", lot_type: str = "") -> str:
    links = build_verification_links(cadastral, address, vin, lot_type)
    label = "проверки по авто" if lot_type == "авто" else "проверка арестов/обременений"
    extra = f"{label}:\n{links}"
    return f"{wtc}\n{extra}" if wtc else extra


def _cadastral_value_from_lot(lot: dict) -> int:
    """Кадастровая стоимость из ЕГРН или текста отчёта."""
    try:
        from appraisal_pdf import _extract_cadastral_value
    except ImportError:
        _extract_cadastral_value = None  # type: ignore[assignment]
    for blob in (lot.get("appraisal_pdf_text"), lot.get("egrn_pdf_text")):
        if blob and len(blob) > 100 and _extract_cadastral_value:
            v = _extract_cadastral_value(blob)
            if v:
                return v
    for rec in dedupe_egrn_records(lot.get("egrn_records") or []):
        cv = int(rec.get("cadastral_value") or 0)
        if cv > 0:
            return cv
    return 0


def _collect_document_text(lot: dict) -> str:
    """Весь распознанный текст документов (длиннейший ЕГРН + отчёт)."""
    parts: list[str] = []
    if lot.get("appraisal_pdf_text"):
        parts.append(lot["appraisal_pdf_text"])
    if lot.get("egrn_pdf_text"):
        parts.append(lot["egrn_pdf_text"])
    return "\n\n".join(parts)


def _resolve_market_from_documents(lot: dict, area_sqm: float, lot_type: str) -> dict:
    """Рыночный ориентир: таблица аналитики торгов → оценка из отчёта. Без кадастра."""
    text = _collect_document_text(lot)
    try:
        from appraisal_pdf import compute_trading_market, parse_trading_analytics, parse_appraisal_pdf
    except ImportError:
        return {
            "market_price": 0, "manual_market": True, "comment": "рынок не определён",
            "area": area_sqm, "market_source": "",
        }

    if text and ("аналитик" in text.lower() and "торг" in text.lower()):
        lot["trading_analytics"] = parse_trading_analytics(text)

    exclude: set[int] = set()
    for rec in lot.get("egrn_records") or []:
        cv = int(rec.get("cadastral_value") or 0)
        if cv > 0:
            exclude.add(cv)

    ta_mkt = compute_trading_market(lot_type, area_sqm, text, exclude_prices=exclude)
    if ta_mkt.get("market_price", 0) > 0:
        return {
            "market_price": ta_mkt["market_price"],
            "rental_monthly": 0,
            "price_per_sqm": ta_mkt.get("median_ppm", 0),
            "area": area_sqm,
            "comment": ta_mkt.get("comment", "")[:200],
            "manual_market": False,
            "market_source": "trading_analytics",
            "median_ppm": ta_mkt.get("median_ppm", 0),
            "n_analogs": ta_mkt.get("n_analogs", 0),
            "market_coarse": ta_mkt.get("coarse", False),
            "market_disclaimer": ta_mkt.get("market_disclaimer", ""),
            "trading_analogs": ta_mkt.get("analogs", []),
        }

    appr = lot.get("appraisal_parsed") or {}
    if not appr.get("market_price") and text:
        appr = parse_appraisal_pdf(text)
    mp = int(appr.get("market_price") or appr.get("appraisal_price") or 0)
    if mp > 0:
        return {
            "market_price": mp,
            "rental_monthly": 0,
            "price_per_sqm": int(mp / area_sqm) if area_sqm else 0,
            "area": area_sqm,
            "comment": "из отчёта об оценке",
            "manual_market": False,
            "market_source": "appraisal_valuation",
        }

    return {
        "market_price": 0,
        "rental_monthly": 0,
        "price_per_sqm": 0,
        "area": area_sqm,
        "comment": "рынок не определён",
        "manual_market": True,
        "market_source": "",
    }


def format_price_line(an: dict) -> str:
    """Строка цены — без ложного дисконта при неизвестном рынке."""
    price = an.get("price", "—")
    lot_type = an.get("lot_type", "")
    if not an.get("market_known"):
        if lot_type == "авто":
            return f"💰 {price} | _оценить рынок авто вручную_"
        if lot_type == "земля":
            return f"💰 {price} | _оценить рынок земли вручную_"
        return f"💰 {price} | _рынок не определён — оценить вручную_"
    disc = an.get("discount_pct", "0")
    disc_s = f", дисконт ~{disc}%" if str(disc) not in ("0", "?", "") else ""
    src = an.get("market_source", "")
    mkt = an.get("market_price", "—")
    if src == "trading_analytics":
        ppm = an.get("median_ppm") or 0
        ppm_s = f"{ppm:,} ₽".replace(",", " ") if ppm else "?"
        n = an.get("n_analogs") or 0
        coarse = an.get("market_coarse")
        coarse_s = " (мало аналогов, ориентир грубый)" if coarse else ""
        return (
            f"💰 цена лота {price}, похожие лоты рядом ~{ppm_s}/м² "
            f"(медиана по {n} аналогам) → ориентир {mkt}{disc_s}{coarse_s} "
            f"_по аналогичным лотам рядом (аналитика торгов)_"
        )
    if src == "appraisal_comparables":
        cr = an.get("comparables_range") or "—"
        return (
            f"💰 цена лота {price}, аналоги рядом {cr} (мед. {mkt}){disc_s} "
            f"_ориентир по объявлениям из отчёта, проверить_"
        )
    if src == "cadastral_coarse":
        return f"💰 {price} | _рынок не определён — кадастр не используется_"
    if src in ("appraisal", "appraisal_valuation"):
        return f"💰 {price} → оценка {mkt}{disc_s} _из отчёта_"
    if src == "search":
        return f"💰 {price} → ориентир {mkt}{disc_s} _проверить_"
    return f"💰 {price} → рынок {mkt}{disc_s}"


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


def _extract_pdf_text_safe(raw: bytes) -> tuple[str, str]:
    try:
        from egrn_pdf import extract_pdf_text
        return extract_pdf_text(raw)
    except ImportError:
        pass
    except Exception as e:
        log.debug("egrn_pdf extract: %s", e)
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            parts = [(p.extract_text() or "") for p in pdf.pages[:8]]
        text = "\n".join(parts).strip()
        if len(text) > 80:
            return text, "text"
    except Exception as e:
        log.debug("pdfplumber extract: %s", e)
    return "", "failed"


async def download_pdf(lot_id: str) -> str:
    try:
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
                    if resp.status_code == 200 and b"%PDF" in resp.content[:10]:
                        text, method = _extract_pdf_text_safe(resp.content)
                        if text and len(text) > 80:
                            print(f"    📄 PDF ({method}, {len(text)} симв.)")
                            return text
                except Exception:
                    continue
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
        tl = text.lower()
        if any(x in tl for x in ("егрн", "egrn", "выписк", "выписка егрн")):
            details["has_egrn_on_site"] = True
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

    prompt = f"""Ты помощник по банкротным торгам. Анализируй строго как {lot_type.upper()}.
НЕ давай рекомендаций «бери/не бери», «отличная инвестиция», процентов доходности.
Только факты из документов и честно отмечай, что неизвестно.

═══ ОБЪЕКТ ═══
Тип: {lot_type.upper()}
Название: {title[:200]}
Регион: {region_name}
Цена торгов: {f'{lot_price:,.0f}₽' if lot_price else 'не определена'}
Рыночная цена: {f'{market_price:,.0f}₽' if market_price else 'не определена'}
Дисконт к рынку: {disc_pct}%
Участников: {parts_n}
{step_info}
{f'Кадастр: {cadastral}' if cadastral else 'Кадастр: не найден'}
{f'VIN: {vin}' if vin else ''}
Документы: {'есть — опирайся на них' if has_docs else 'не получены — не выдумывай юр. статус'}

═══ ДОКУМЕНТЫ ═══
{all_docs if all_docs else 'Документы не получены.'}

Ответь ТОЛЬКО JSON:
{{
  "legal_summary": "кратко: собственник, обременения, аресты — только если есть в тексте",
  "encumbrances": "нет / есть (что именно) / не указано в документах",
  "owners_count": "число или не указано",
  "legal_risks": ["факт из документов"],
  "invest_risks": ["факт или неизвестное"],
  "invest_opportunities": ["факт из документов, без оценочных суждений"],
  "invest_potential": "не оценивается",
  "risk_level": "низкий / средний / высокий / не определён",
  "liquidity_level": "не оценивается",
  "liquidity_days": 0,
  "exit_strategy": "",
  "strategy": "нейтрально: что известно и чего не хватает для решения",
  "what_to_check": "3-5 пунктов проверки вручную для {lot_type}",
  "action": "ПРОВЕРИТЬ ДОКУМЕНТЫ",
  "verdict": "сводка фактов без рекомендации покупать"
}}
Запрещено: БРАТЬ, НЕ БРАТЬ, РЕКОМЕНДУЮ, заработаешь, отличная инвестиция."""

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


def minimal_lot_analysis(lot: dict) -> dict:
    """Минимальная карточка, если полный analyze_lot упал."""
    lot_type = lot.get("category") or detect_type(
        f"{lot.get('title', '')} {lot.get('description', '')[:500]}"
    )
    lot_price = float(lot.get("price") or 0)
    title = lot.get("title_full") or lot.get("title", "")

    def fmt(p):
        try:
            p = float(p)
            if p >= 1_000_000:
                return f"{p / 1_000_000:.1f} млн ₽"
            if p > 0:
                return f"{int(p):,} ₽".replace(",", " ")
        except (TypeError, ValueError):
            pass
        return "уточните на сайте"

    an_stub = {
        "price": fmt(lot_price),
        "market_price": "не определён",
        "land_manual_market": lot_type == "земля",
        "market_known": False,
        "market_source": "",
        "discount_pct": "?",
        "lot_type": lot_type,
    }
    trading = build_trading_summary(lot)
    return {
        "lot_type": lot_type,
        "total_score": 6.0,
        "score_label": "📊 6.0/10",
        "price": an_stub["price"],
        "market_price": "не определён",
        "market_known": False,
        "market_comment": "",
        "discount_pct": "?",
        "discount_ok": False,
        "qualifies_hot": False,
        "lot_price_raw": lot_price,
        "market_price_raw": 0,
        "price_line": format_price_line(an_stub),
        "step": "",
        "legal_text": "",
        "document_status": resolve_document_status(lot),
        "auto_summary": "",
        "trading_summary": trading,
        "action_emoji": "🟡",
        "verdict_label": "Смотреть",
        "verdict_detail": "анализ упрощён — проверьте документы",
        "action": "ПРОВЕРИТЬ ДОКУМЕНТЫ",
        "risk_level": "🟡",
        "risk_score": 50,
        "invest_score": 50,
        "red_flags_text": "",
        "title_hint": title[:70],
    }


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
        red_flags = list(dict.fromkeys(red_flags + (lot.get("appraisal_flags") or [])))
    else:
        red_flags = scan_red_flags(full_text_src, lot_type) if is_real_estate(lot_type) else []
        if lot.get("appraisal_flags"):
            red_flags = list(dict.fromkeys(red_flags + lot["appraisal_flags"]))

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
    elif light:
        mkt = {
            "market_price": 0, "manual_market": True,
            "comment": "", "area": lot.get("area_sqm") or 0,
        }
    else:
        area_sqm = lot.get("area_sqm") or 0
        if not area_sqm:
            src_area = f"{title} {page_text} {egrn_pdf}"
            am = re.search(r"(\d+[.,]?\d*)\s*(?:м²|кв\.?\s*м)", src_area, re.I)
            if am:
                try:
                    area_sqm = float(am.group(1).replace(",", "."))
                except ValueError:
                    area_sqm = 0
        appr = lot.get("appraisal_parsed") or {}
        mkt = _resolve_market_from_documents(lot, area_sqm, lot_type)
        if mkt.get("market_price", 0) <= 0:
            orient = {"found": False, "comment": "рынок не определён"}
            try:
                from market_search import fetch_market_orientir
                orient = await fetch_market_orientir(
                    lot_type, lot.get("address", ""), area_sqm, region, title,
                )
            except Exception as e:
                log.warning("market_search unavailable: %s", e)
            if orient.get("found"):
                mkt = {
                    "market_price": orient["market_price"],
                    "rental_monthly": 0,
                    "price_per_sqm": orient.get("price_per_sqm", 0),
                    "area": area_sqm,
                    "comment": orient["comment"],
                    "manual_market": False,
                    "market_source": "search",
                }
            elif not mkt.get("comment"):
                mkt = {
                    "market_price": 0, "rental_monthly": 0, "price_per_sqm": 0,
                    "area": area_sqm,
                    "comment": orient.get("comment", "рынок не определён"),
                    "manual_market": True,
                }
    mkt_prc = mkt["market_price"]
    market_known = not mkt.get("manual_market") and mkt_prc > 0

    disc_pct = 0
    if market_known and lot_price > 0 and mkt_prc > lot_price:
        disc_pct = round((mkt_prc - lot_price) / mkt_prc * 100)
        if disc_pct >= 85:
            disc_pct = 0
            mkt_prc = 0
            market_known = False

    has_pdf = bool(lot.get("egrn_read_ok"))
    document_status = resolve_document_status(lot)
    egrn_parsed = lot.get("egrn_parsed") or {}
    has_pdf = has_pdf or bool(egrn_parsed.get("parsed_ok"))

    if lot_type == "авто":
        legal_text = ""
        encumb_from_egrn = ""
    elif egrn_parsed.get("parsed_ok"):
        legal_text = format_egrn_legal_block(egrn_parsed)
        encumb_from_egrn = _summarize_egrn_encumbrances(lot) or _clean_field_text(
            egrn_parsed.get("encumbrances", ""), 100,
        )
    elif lot.get("egrn_ocr_failed"):
        legal_text = ""
        encumb_from_egrn = ""
    elif lot.get("pdf_from_egrn") and egrn_pdf:
        legal_text = "ЕГРН скачан — ключевые поля не распознаны"
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
        except Exception:
            log.exception("Groq failed for lot %s", lot.get("id", ""))
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
        extra.append(f"Дисконт {disc_pct}% — причина не объяснена в документах")
    elif market_known and disc_pct >= 40:
        extra.append(f"Дисконт {disc_pct}% — сверить с отчётом об оценке")
    if is_real_estate(lot_type) and cadastral:
        cad_line = f"🏛 Кадастр: {cadastral}"
        if rosreestr:
            cad_line += f"\n   📍 {rosreestr}"
        extra.append(cad_line)
    if lot_type == "авто" and vin:
        extra.append(f"🔍 VIN: {vin}")
    if parts_n == 0:
        extra.append("Нет заявок на момент анализа")
    elif parts_n == 1:
        extra.append("1 участник")
    elif parts_n <= 3:
        extra.append(f"{parts_n} участника")
    elif parts_n > 5:
        extra.append(f"{parts_n} участников — высокая конкуренция")
    valid_risks = [r for r in risks if r not in
                   ("нет данных", "документы не получены — проверьте перед покупкой",
                    "требует проверки", "стандартные риски")]
    valid_opps = [o for o in opps if o not in ("требует анализа",)]
    if market_known and valid_risks:
        extra.append("Риски (Groq): " + " | ".join(str(r) for r in valid_risks[:2]))
    if market_known and valid_opps:
        extra.append("Факты (Groq): " + " | ".join(str(o) for o in valid_opps[:2]))
    encumb = encumb_from_egrn or _clean_field_text(expert.get("encumbrances", "") or "", 100)
    exit_s = expert.get("exit_strategy", "") or ""
    strategy = expert.get("strategy", "")
    if not market_known and "Дисконт" in strategy:
        strategy = "Рынок не определён — сравните цену с аналогами вручную."

    partial_an = {
        "lot_price_raw": lot_price,
        "market_price_raw": mkt_prc if market_known else 0,
        "discount_pct": str(disc_pct) if market_known and disc_pct > 0 else "?",
        "land_manual_market": lot_type == "земля" and mkt.get("manual_market", False),
        "market_known": market_known,
        "market_source": mkt.get("market_source", ""),
        "median_ppm": mkt.get("median_ppm", 0),
        "n_analogs": mkt.get("n_analogs", 0),
        "market_coarse": mkt.get("market_coarse", False),
    }
    from verdict import run_verdict_pipeline
    try:
        vr = run_verdict_pipeline(lot, partial_an)
    except Exception:
        log.exception("verdict pipeline failed for lot %s", lot.get("id", ""))
        vr = {
            "verdict_label": "Риск по документам: не определён",
            "verdict_detail": "вердикт не рассчитан",
            "verdict_card": "",
            "risk_score": 50,
            "risk_level": "🟡",
            "document_risk_level": "средний",
            "verdict_pluses": [],
            "verdict_minuses": [],
            "facts_json": {},
            "manual_checks": [],
            "key_flags": [],
        }
    verdict_label = vr["verdict_label"]
    verdict_emoji = vr.get("risk_level") or "🟡"

    an_stub = {
        "price": fmt(lot_price),
        "market_price": fmt(mkt_prc, is_market=True) if market_known else "не определён",
        "land_manual_market": mkt.get("manual_market", False),
        "market_known": market_known,
        "market_source": mkt.get("market_source", ""),
        "discount_pct": str(disc_pct) if market_known and disc_pct > 0 else "?",
        "lot_type": lot_type,
        "comparables_range": mkt.get("comparables_range", ""),
        "market_disclaimer": mkt.get("market_disclaimer", ""),
        "median_ppm": mkt.get("median_ppm", 0),
        "n_analogs": mkt.get("n_analogs", 0),
        "market_coarse": mkt.get("market_coarse", False),
    }
    discount_ok = market_known and disc_pct >= MIN_DISCOUNT_PCT

    result = {
        "lot_type":       lot_type,
        "total_score":    score,
        "score_label":    f"{'🔥' if score>=9 else '⭐' if score>=8 else '📊'} {score}/10",
        "price":          an_stub["price"],
        "market_price":   an_stub["market_price"],
        "market_known":   market_known,
        "market_source":  mkt.get("market_source", ""),
        "market_comment": mkt.get("comment", ""),
        "comparables_range": mkt.get("comparables_range", ""),
        "market_disclaimer": mkt.get("market_disclaimer", ""),
        "median_ppm": mkt.get("median_ppm", 0),
        "n_analogs": mkt.get("n_analogs", 0),
        "market_coarse": mkt.get("market_coarse", False),
        "discount_pct":   an_stub["discount_pct"],
        "discount_ok":    discount_ok,
        "hot_label":      market_known and disc_pct >= HOT_LABEL_PCT,
        "qualifies_hot":  market_known and disc_pct >= HOT_LABEL_PCT and score >= 7.0,
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
        "risk_text":      f"документы: {vr.get('document_risk_level', 'средний')}",
        "invest_text":    "",
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
        "assessment_score": vr.get("assessment_score"),
        "assessment_logic": vr.get("assessment_logic", ""),
        "invest_score": vr.get("assessment_score"),
        "document_risk_level": vr.get("document_risk_level"),
        "verdict_pluses": vr.get("verdict_pluses", []),
        "verdict_minuses": vr.get("verdict_minuses", []),
        "verdict_label": verdict_label,
        "verdict_detail": vr["verdict_detail"],
        "verdict_card": vr["verdict_card"],
        "manual_checks": vr["manual_checks"],
    }
    result.update(compute_digest_metrics(lot, result))
    return result
